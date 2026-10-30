"""Codex zero-usage, paid-credit and queued-move episodes."""

from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.whoami import quota_alert_test_support as support
import quota_alert
from push_texts import RenderedPush, render
from scripts.whoami import agent_accounts
from agent_accounts import ClaudeAccountsInUse, Quota
import usage_store
from scripts.accounts import accounts, moves, next_moves, refusals, role_map
from user_action import ActionRequired

from scripts.whoami.quota_alert_test_support import REGISTRY

setUpModule = support.setUpModule


class CodexZeroEpisodeTests(support.CodexZeroEpisodeFixture):
    now: datetime

    def test_queued_claude_account_routes_workers_and_overrides_only_worker_launches(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude4"), [])
        directory = accounts.account_dir("claude4")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "workers@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        self.swap.assert_not_called()
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.claude_account, next_moves.ClaudeWorkersTarget("claude4"))
        routed = [row for row in moves.read_journal() if row["kind"] == "routed to claude"]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0].get("claude_account"), "claude4")
        self.assertEqual(role_map.read_role_map().workers_claude, role_map.NamedWorkersClaudeAccount("claude4", self.now + timedelta(days=2)))
        # A caller with an invalid account must not replace the workers route.
        selected = accounts.account_for_unscoped_launch({"CLAUDE_CONFIG_DIR": str(self.root / ".claude99")}, workers=True)
        self.assertEqual(selected, accounts.NamedAccount("claude4", directory))
        with self.assertRaises(accounts.AccountsError):
            _ = accounts.account_for_unscoped_launch({"CLAUDE_CONFIG_DIR": str(self.root / ".claude99")})
        (directory / ".claude.json").unlink()
        with self.assertRaisesRegex(accounts.AccountsError, "claude and /login"):
            _ = accounts.account_for_unscoped_launch({}, workers=True)

        # Recovery closes the shared route as well as the local fallback.
        with quota_alert.state_file() as state:
            back = quota_alert.switch_back(state, self.now + timedelta(days=8),
                self.readings(self.now + timedelta(days=8), left=100))
        self.assertIsInstance(back, quota_alert.SwitchBack)
        self.assertIsInstance(role_map.read_role_map().workers_claude, role_map.DefaultWorkersClaudeAccount)

    def test_failed_named_claude_switch_keeps_the_previous_workers_route(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "workers@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        with mock.patch.object(quota_alert, "edit_registry", return_value="fixture switch failed"):
            _ = self.alert(left=1)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertIsInstance(role_map.read_role_map().workers_claude, role_map.DefaultWorkersClaudeAccount)
        self.assertEqual(len(next_moves.read_next_moves()), 1)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_failed_route_rollback_keeps_both_errors_and_the_unseen_switch_notice(self) -> None:
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        until = self.now + timedelta(days=2)
        note = self.note(1)
        original_write = role_map.write_role_map
        for failure in (role_map.RoleMapWriteRefused("fixture rollback refused"),
                        role_map.RoleMapUnavailable("fixture map unavailable"),
                        OSError("fixture disk full")):
            with self.subTest(failure=type(failure).__name__):
                original_write(role_map.WorkersClaudeRouteChange(
                    role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "reset the test route", self.now.isoformat()))

                def write_route(change: role_map.RoleMapChange | role_map.WorkersClaudeRouteChange) -> None:
                    if isinstance(change, role_map.WorkersClaudeRouteChange) and isinstance(
                            change.account, role_map.DefaultWorkersClaudeAccount):
                        raise failure
                    original_write(change)

                with quota_alert.state_file() as state:
                    state["episodes"][note.label] = {"since": self.now.isoformat(), "last": {}}
                    state["unseen"] = []
                    with (mock.patch.object(quota_alert, "edit_registry", return_value="fixture edit failed"),
                          mock.patch.object(role_map, "write_role_map", side_effect=write_route)):
                        result = quota_alert.switch_to_claude(state, note, self.now.isoformat(), owner,
                            hold=quota_alert.ScheduledMoveHold(until),
                            claude_account=next_moves.ClaudeWorkersTarget("claude4"))
                    assert isinstance(result, quota_alert.CodexWorkSwitchFailed)
                    self.assertIn("fixture edit failed", result.cause)
                    self.assertIn(f"route rollback failed: {failure}", result.cause)
                    self.assertEqual(state["episodes"][note.label].get("switch_failed"), result.cause)
                with quota_alert.state_file() as saved:
                    unseen = saved.get("unseen", [])
                    self.assertEqual(len(unseen), 1)
                    self.assertIn(result.cause, unseen[0])
                self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_uncertain_registry_switch_keeps_the_named_route_until_the_weekly_reset(self) -> None:
        for failed_step in ("edit", "confirmation"):
            with self.subTest(failed_step=failed_step):
                _ = write_registry(quota_alert.REGISTRY, base_text=REGISTRY)
                owner = self.readings(self.now).selected_account
                assert isinstance(owner, agent_accounts.KnownCodexAccount)
                until = self.now + timedelta(days=2)
                note = self.note(1)
                original_edit = quota_alert.edit_registry

                def edit_then_lose_confirmation(editor: str, *arguments: str) -> str | None:
                    result = original_edit(editor, *arguments)
                    if failed_step == "edit":
                        raise OSError("fixture edit reply lost")
                    return result

                original_read = quota_alert.registry_assignments
                with quota_alert.state_file() as state:
                    with (mock.patch.object(quota_alert, "edit_registry", side_effect=edit_then_lose_confirmation),
                          mock.patch.object(quota_alert, "registry_assignments", side_effect=[
                              original_read(), OSError("fixture confirmation failed")])):
                        result = quota_alert.switch_to_claude(state, note, self.now.isoformat(), owner,
                            hold=quota_alert.ScheduledMoveHold(until),
                            claude_account=next_moves.ClaudeWorkersTarget("claude4"))
                    self.assertIsInstance(result, quota_alert.CodexWorkSwitchFailed)
                    self.assertTrue(any("failed" in line for line in state.get("unseen", [])))
                self.assertEqual(original_read()["direct"], "claude")
                self.assertEqual(role_map.read_role_map().workers_claude,
                                 role_map.NamedWorkersClaudeAccount("claude4", until))

    def test_lost_quota_state_does_not_keep_the_named_workers_account_past_its_reset(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        for label in ("claude2", "claude4"):
            _ = (self.root / f"{label}.md").write_text(f"---\nlogin: {label}@example.invalid\n---\n")
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        caller = {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2"))}
        self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                         accounts.NamedAccount("claude4", accounts.account_dir("claude4")))
        _ = quota_alert.STATE.write_text("invalid quota state")
        until = self.now + timedelta(days=2)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            self.assertIsInstance(quota_alert.switch_back(state, until, self.readings(until, left=100)),
                                  quota_alert.NoCodexFallbackRestoration)
        with mock.patch.object(accounts, "datetime", wraps=datetime, now=mock.Mock(return_value=until)):
            self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                             accounts.NamedAccount("claude2", accounts.account_dir("claude2")))

    def test_queued_claude_account_with_no_folder_waits_with_setup_step(self) -> None:
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        head = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertTrue(any("accounts.py setup claude4" in text for text in self.relayed_texts()))

    def test_stale_workers_head_blocks_a_fresh_later_entry_and_notifies_once(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        later = next_moves.add_next_move("workers", 1, "codex2")
        with (mock.patch.object(quota_alert, "recipients", return_value=["director"]),
              mock.patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check,
              redirect_stdout(io.StringIO()) as output):
            for minute in (0, 2):
                now = self.now + timedelta(minutes=minute)
                returned = quota_alert.run_workers_next_moves(self.stale_workers_target_readings(now), now)
                self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex1", self.root / ".codex1"))
        self.swap.assert_not_called()
        self.assertEqual([call.args[0] for call in check.call_args_list], ["codex3", "codex3"])
        self.assertEqual(next_moves.read_next_moves(), (head, later))
        self.assertEqual(moves.read_moves(), [])
        self.relay.assert_called_once()
        recipient, text, _ = cast(tuple[str, str, str | None], self.relay.call_args.args)
        self.assertEqual(recipient, "director")
        self.assertNotIn(head.id, text)
        for detail in ("workers 1", "codex3", "due", "no fresh reading",
                       (self.now - timedelta(minutes=11)).strftime("%H:%M")):
            self.assertIn(detail, text)
        self.assertIn(text, output.getvalue())
        self.phone.assert_not_called()

    def test_queued_workers_head_blocks_a_provider_refusal_fallback_swap(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        readings = self.readings(self.now, left=80)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.object(accounts, "pick", wraps=accounts.pick) as pick:
            _ = quota_alert.blocked([self.note(1, left=80)], identity, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        pick.assert_not_called()
        self.swap.assert_not_called()
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        waits = moves.read_moves()
        self.assertEqual([row["kind"] for row in waits], ["waited"])
        waited = waits[0]
        assert waited["kind"] == "waited"
        self.assertEqual((waited["window"], waited["subject"]), ("weekly", "workers"))
        self.assertIn(refusals.key("codex", identity.login), refusals.load())

    def test_queued_workers_head_blocks_a_credits_off_fallback_swap(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        notes = [self.note(1, "off"), self.note(2), self.note(3)]
        with (mock.patch.object(quota_alert, "recipients", return_value=["director"]),
              redirect_stdout(io.StringIO())):
            _ = quota_alert.alert(notes, self.now,
                selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(
                    self.root / ".claude", "fixture identity unavailable"), False, (), frozenset()),
                codex_readings=self.stale_workers_target_readings(self.now))
        self.swap.assert_not_called()
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertEqual(moves.read_moves(), [])

    def test_removing_a_queued_head_releases_the_still_zero_credits_off_fallback_once(self) -> None:
        _ = self.note(1, "off")
        head = next_moves.add_next_move("workers", 1, "codex1")
        _ = self.alert(spare_left=50)
        self.swap.assert_not_called()
        self.assertEqual(next_moves.read_next_moves(), (head,))
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        _ = self.alert(self.now + timedelta(minutes=4), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertEqual(len([row for row in moves.read_moves() if row["kind"] == "requested"]), 1)

    def test_workers_target_allowance_changes_below_the_same_threshold_send_one_notice(self) -> None:
        head = next_moves.add_next_move("workers", 20, "codex3")
        with mock.patch.object(quota_alert, "recipients", return_value=["director"]):
            for minute, left in ((0, 10), (2, 9)):
                now = self.now + timedelta(minutes=minute)
                readings = self.readings(now, left=1, spare_accounts=((3, left, "0"),))
                _ = quota_alert.run_workers_next_moves(readings, now)
        self.relay.assert_called_once()
        text = cast(str, self.relay.call_args.args[1])
        self.assertIn("10% left, at or below 20%", text)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_workers_successor_becomes_eligible_only_after_its_head_is_consumed(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        later = next_moves.add_next_move("workers", 80, "codex2")
        readings = self.readings(self.now, left=1,
            spare_accounts=((3, 50, "0"), (2, 90, "0")))
        with mock.patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check:
            returned = quota_alert.run_workers_next_moves(readings, self.now)
            self.swap.assert_called_once_with("workers", "codex3", why=f"auto: {head.id}", cause="auto")
            self.assertEqual(next_moves.read_next_moves(), (later,))
            self.assertEqual([call.args[0] for call in check.call_args_list], ["codex3"])
            self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex3", self.root / ".codex3"))
            self.swap.reset_mock()
            check.reset_mock()
            now = self.now + timedelta(minutes=2)
            readings = self.readings(now, selected=3, left=50,
                spare_accounts=((2, 90, "0"),))
            returned = quota_alert.run_workers_next_moves(readings, now)
        self.swap.assert_called_once_with("workers", "codex2", why=f"auto: {later.id}", cause="auto")
        self.assertEqual([call.args[0] for call in check.call_args_list], ["codex2"])
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex2", self.root / ".codex"))
        self.assertEqual([row["why"] for row in moves.read_moves() if row["kind"] == "requested"],
                         [f"auto: {head.id}", f"auto: {later.id}"])
        self.relay.assert_not_called()

    def test_old_paid_notices_are_rebuilt_before_retry_from_the_current_reading(self) -> None:
        from push_texts import render
        for suffix in ("_credits", "_credits_last"):
            for queued in (False, True):
                with self.subTest(suffix=suffix, queued=queued):
                    quota_alert.STATE.unlink(missing_ok=True)
                    next_moves.next_moves_file().unlink(missing_ok=True)
                    _ = self.alert()
                    with quota_alert.state_file() as state:
                        episode = state.get("codex_zero", {})["codex1"]
                        episode["phone"] = "due"
                        episode["notice"] = quota_alert.ZeroEpisodeNotice(
                            id="quota." + "codex_on" + suffix, values={"file": "codex 1.md"})
                        opened = episode["since"]
                    if queued:
                        _ = next_moves.add_next_move("workers", 1, "codex1")
                    self.phone.reset_mock()
                    self.relay.reset_mock()
                    _ = self.alert(self.now + timedelta(minutes=2), balance="6310")
                    push, = self.zero_pushes()
                    expected_id = "quota.paid_credits_queued" if queued else "quota.paid_credits"
                    self.assertEqual(push.id, expected_id)
                    self.assertEqual(push, render(expected_id, **push.values))
                    self.assertEqual(push.values["credits"], "6,310")
                    with quota_alert.state_file() as state:
                        episode = state.get("codex_zero", {})["codex1"]
                        self.assertEqual(episode["since"], opened)
                        self.assertEqual(episode["notice"]["id"], "quota.paid_credits")
                        self.assertNotEqual(episode["phone"], "due")

    def test_old_paid_notice_keeps_the_earlier_reset_of_a_paid_only_spare(self) -> None:
        from push_texts import render

        _ = self.alert(selected=2, spare_accounts=((1, 0, "6370"),))
        fresh, = self.zero_pushes()
        earlier_reset = quota_alert.reset_display(refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual(fresh.values["next_reset"], earlier_reset)
        self.assertNotEqual(fresh.values["refill"], earlier_reset)
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex2"]
            episode["phone"] = "due"
            episode["notice"] = quota_alert.ZeroEpisodeNotice(
                id="quota." + "codex_on" + "_credits_last", values={"file": "codex2.md"})
        self.phone.reset_mock()

        _ = self.alert(self.now + timedelta(minutes=2), selected=2, spare_accounts=((1, 0, "6370"),))

        rebuilt, = self.zero_pushes()
        self.assertEqual(rebuilt, fresh)
        self.assertEqual(rebuilt, render("quota.paid_credits", **rebuilt.values))
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex2"]["notice"]["values"]["next_reset"], earlier_reset)
        self.swap.assert_not_called()

    def test_legacy_episode_keys_continue_without_a_second_opening_alert(self) -> None:
        _ = self.alert()
        original = quota_alert.STATE.read_text()
        _ = quota_alert.STATE.write_text(original.replace('"codex1":', '"codex 1":'))
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2))
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state.get("codex_zero", {}))
            self.assertNotIn("codex 1", state.get("codex_zero", {}))
            state["unseen"] = ["next authorized write"]
        self.assertNotIn('"codex 1":', quota_alert.STATE.read_text())

    def test_legacy_pending_notice_retry_changes_only_account_spelling(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            episode["phone"] = "due"
            notice = episode["notice"]
            notice["values"]["credits"] = "codex 1 :: preserve  two spaces; claude 2, codex 2!\nNext line."
            expected = render(notice["id"], **{**notice["values"],
                "credits": "codex1 :: preserve  two spaces; claude2, codex2!\nNext line."})
            episode["forecast_notice"] = quota_alert.ZeroEpisodeNotice(id="quota.codex_credits_forecast", values={
                "account": "codex 1", "horizon": "until 03:00", "forecast": "codex 1:  70 credits; keep punctuation!",
                "destination": "then claude 2 / codex 2\nnext"})
            opened = episode["since"]
        _ = self.alert(self.now + timedelta(minutes=2))
        self.phone.assert_called_once_with(expected)
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["since"], opened)
            self.assertEqual(episode["notice"]["values"]["credits"],
                             "codex1 :: preserve  two spaces; claude2, codex2!\nNext line.")
            assert "forecast_notice" in episode
            self.assertEqual(episode["forecast_notice"]["values"], {
                "account": "codex1", "horizon": "until 03:00", "forecast": "codex1:  70 credits; keep punctuation!",
                "destination": "then claude2 / codex2\nnext"})

    def test_no_room_uses_the_real_paid_credits_template_once(self) -> None:
        from push_texts import catalog, render_entry, user_send_argv
        entry = next(entry for entry in catalog() if entry.id == "quota.paid_credits")
        _ = self.alert()
        _ = self.alert(self.now + timedelta(minutes=2))
        push, = self.zero_pushes()
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertEqual(set(push.values), {"sender", "account", "machine", "credits", "refill", "next_reset"})
        self.assertEqual(push.values["sender"], "quota_alert")
        self.assertEqual(push, render_entry(entry, push.values))
        self.assertEqual(push.values["account"], "codex1")
        self.assertIn("6,370", push.values["credits"])
        reset = quota_alert.reset_display(refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual(push.values["refill"], reset)
        self.assertEqual(push.values["next_reset"], reset)
        argv = user_send_argv(push)
        self.assertIn("quota.paid_credits", argv)
        self.assertIn("--text-id", argv)
        for key, value in push.values.items():
            self.assertIn(f"{key}={value}", argv)
        self.swap.assert_not_called()

    def test_real_paid_credits_notice_keeps_the_queued_move_in_control(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        with mock.patch.object(quota_alert, "switch_to_claude", side_effect=AssertionError("queued credits routed work")):
            log = self.alert()
            _ = self.note(1, "off")
            _ = self.alert(self.now + timedelta(minutes=2))
        push, = self.zero_pushes()
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertEqual(push.id, "quota.paid_credits_queued")
        self.assertNotIn("next_reset", push.values)
        self.assertNotIn("Claude", push.body)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.swap.assert_not_called()

    def test_unmoved_included_room_during_an_open_fallback_uses_paid_credits(self) -> None:
        from push_texts import render
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            state["fallback"] = quota_alert.AccountCodexQuotaFallback(owner, self.now.isoformat(), ("delegate",))
        _ = self.alert(spare_left=50)
        push, = self.zero_pushes()
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertEqual(push, render("quota.paid_credits", **push.values))
        self.swap.assert_not_called()

    def test_credits_on_moves_to_room_without_a_phone_or_session_notice(self) -> None:
        _ = self.alert(selected=2, spare_accounts=((1, 100, "0"),))
        self.swap.assert_called_once()
        self.assertEqual(self.swap.call_args.args, ("workers", "codex1"))
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex2", state.get("codex_zero", {}))

    def test_zero_accounts_with_only_paid_credits_keep_the_workers_on_one_account(self) -> None:
        for minute in (0, 2, 4):
            _ = self.alert(self.now + timedelta(minutes=minute),
                           spare_accounts=((2, 0, "6370"), (3, 0, "6370")))
        self.swap.assert_not_called()
        push, = self.zero_pushes()
        self.assertEqual(push.values["account"], "codex1")
        self.assertFalse(any(row["kind"] == "requested" for row in moves.read_moves()))

    def test_open_paid_episode_moves_to_new_included_room_on_the_next_pass(self) -> None:
        _ = self.alert()
        self.swap.assert_not_called()
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2), spare_accounts=((2, 100, "0"),))
        self.swap.assert_called_once_with("workers", "codex2",
            why="codex1 weekly usage used; move to room", cause="auto", included_only=True)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_credits_on_does_not_move_to_a_paid_only_five_hour_window(self) -> None:
        _ = self.alert(spare_accounts=((2, 50, "6370"),), spare_five_hour=((2, 0),))
        self.swap.assert_not_called()
        self.assertEqual(len(self.zero_pushes()), 1)

    def test_credits_turned_on_during_a_queued_wait_use_room_when_the_queue_is_removed(self) -> None:
        _ = self.note(1, "off")
        head = next_moves.add_next_move("workers", 1, "codex1")
        _ = self.alert(spare_left=50)
        _ = self.note(1, "on")
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=4), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2",
            why="codex1 weekly usage used; move to room", cause="auto", included_only=True)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_credit_allowance_sends_one_phone_push_and_logs_without_session_delivery(self) -> None:
        log = self.alert()
        self.assertEqual(len(self.zero_pushes()), 1)
        push = self.zero_pushes()[-1]
        self.relay.assert_not_called()
        self.assertIn("6,370", push.body)
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertIn("Claude", push.body)
        self.assertEqual(push.need, "decision")
        self.assertIsInstance(push.action, ActionRequired)
        assert isinstance(push.action, ActionRequired)
        self.assertIn("use_credits: off", push.action.text)
        self.assertIn("codex1.md", push.action.text)
        self.assertIn("natedev", push.action.text)
        self.assertIn("Its plan refills", push.body)
        self.assertIn("paid credits", push.title + push.body)
        for minute in range(2, 32, 2):
            _ = self.alert(self.now + timedelta(minutes=minute))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["credits"], "on")
            self.assertNotEqual(episode["phone"], "due")
            self.assertNotIn("sessions", episode)

    def test_paid_credits_with_a_self_targeting_workers_head_promise_only_the_queue(self) -> None:
        self.assert_queued_credits_wait_without_a_fallback_promise("codex1")

    def test_paid_credits_with_a_blocked_other_workers_head_promise_only_the_queue(self) -> None:
        self.assert_queued_credits_wait_without_a_fallback_promise("codex3")

    def test_queued_workers_credit_forecast_promises_only_the_entry_and_moves_nothing(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        with mock.patch.object(quota_alert, "switch_to_claude", side_effect=AssertionError("queued forecast routed work")):
            _ = self.alert()
            self.phone.reset_mock()
            self.history([(0, 6370), (30, 6340), (60, 6310)])
            _ = self.alert(self.now + timedelta(hours=1), balance="6310")
        forecast, = self.zero_pushes()
        self.assertEqual(forecast.id, "quota.codex_credits_forecast")
        self.assertIn("60", forecast.body)
        self.assertIn("6,310", forecast.body)
        self.assertEqual(forecast.values["destination"],
                         "When credits run out, workers wait for the account move you queued.")
        for forbidden in ("codex2", "codex3", "Claude"):
            self.assertNotIn(forbidden, forecast.body)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()

    def test_failed_workers_blocked_head_delivery_retries_until_it_is_delivered(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        self.relay.side_effect = ["fixture recipient unavailable", None]
        with mock.patch.object(quota_alert, "recipients", return_value=["director"]):
            for minute in (0, 2, 4):
                now = self.now + timedelta(minutes=minute)
                _ = quota_alert.run_workers_next_moves(self.readings(now), now)
                with quota_alert.state_file() as state:
                    self.assertEqual(head.id in state.get("workers_move_notices", {}), minute > 0)
        self.assertEqual(self.relay.call_count, 2)
        self.assertEqual(self.relayed_texts()[0], self.relayed_texts()[1])
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_paid_credit_retry_after_queue_removal_restores_the_never_queued_notice(self) -> None:
        _ = self.alert()
        expected_push, = self.zero_pushes()
        with quota_alert.state_file() as state:
            expected_notice = state.get("codex_zero", {})["codex1"]["notice"]
        for opened_while_queued in (False, True):
            with self.subTest(opened_while_queued=opened_while_queued):
                quota_alert.STATE.unlink()
                usage_store.store_file().unlink(missing_ok=True)
                next_moves.next_moves_file().unlink(missing_ok=True)
                self.phone.reset_mock()
                self.phone.side_effect = [quota_alert.PushFailed("fixture offline"),
                    quota_alert.PushFailed("fixture offline"), quota_alert.PushSent()]
                if opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                _ = self.alert()
                with quota_alert.state_file() as state:
                    self.assertEqual(state.get("codex_zero", {})["codex1"]["notice"], expected_notice)
                if not opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                self.assertEqual(len(next_moves.read_next_moves()), 1)
                _ = self.alert(self.now + timedelta(minutes=2))
                queued_push = self.zero_pushes()[-1]
                self.assertEqual(queued_push.id, "quota.paid_credits_queued")
                self.assertNotIn("next_reset", queued_push.values)
                from push_texts import render
                self.assertEqual(queued_push, render("quota.paid_credits_queued", **queued_push.values))
                self.assertIn("wait for your queued move", queued_push.body)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    self.assertEqual(episode["phone"], "due")
                    self.assertEqual(episode["notice"], expected_notice)
                next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
                _ = self.alert(self.now + timedelta(minutes=4))
                self.assertEqual(self.zero_pushes()[-1], expected_push)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    self.assertNotEqual(episode["phone"], "due")
                    self.assertEqual(episode["notice"], expected_notice)
                self.phone.side_effect = None
        self.swap.assert_not_called()

    def test_credit_forecast_retry_after_queue_removal_restores_the_never_queued_notice(self) -> None:
        self.history([(0, 6370), (30, 6340), (60, 6310)])
        _ = self.alert()
        _ = self.alert(self.now + timedelta(hours=1), balance="6310")
        expected_push = self.zero_pushes()[-1]
        self.assertEqual(expected_push.id, "quota.codex_credits_forecast")
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            assert "forecast_notice" in episode
            expected_notice = episode["forecast_notice"]
        for opened_while_queued in (False, True):
            with self.subTest(opened_while_queued=opened_while_queued):
                quota_alert.STATE.unlink()
                usage_store.store_file().unlink(missing_ok=True)
                next_moves.next_moves_file().unlink(missing_ok=True)
                self.phone.reset_mock()
                self.phone.side_effect = None
                _ = self.alert()
                self.phone.reset_mock()
                self.phone.side_effect = [quota_alert.PushFailed("fixture offline"),
                    quota_alert.PushFailed("fixture offline"), quota_alert.PushSent()]
                if opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                _ = self.alert(self.now + timedelta(hours=1), balance="6310")
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_notice" in episode
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                if not opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                self.assertEqual(len(next_moves.read_next_moves()), 1)
                _ = self.alert(self.now + timedelta(minutes=62), balance="6310")
                self.assertIn("wait for the account move you queued", self.zero_pushes()[-1].body)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_phone" in episode and "forecast_notice" in episode
                    self.assertEqual(episode["forecast_phone"], "due")
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
                _ = self.alert(self.now + timedelta(minutes=64), balance="6310")
                self.assertEqual(self.zero_pushes()[-1], expected_push)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_phone" in episode and "forecast_notice" in episode
                    self.assertNotEqual(episode["forecast_phone"], "due")
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                self.phone.side_effect = None
        self.swap.assert_not_called()

    def test_zero_phone_failure_retries_only_the_phone(self) -> None:
        attempts = 0

        def fail_zero_once(push: RenderedPush) -> quota_alert.PushDelivery:
            nonlocal attempts
            if push.id == "quota.codex_next_move":
                return quota_alert.PushSent()
            attempts += 1
            return quota_alert.PushFailed("offline") if attempts == 1 else quota_alert.PushSent()

        self.phone.side_effect = fail_zero_once
        _ = self.alert()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"]["phone"], "due")
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (2, 0))

    def test_zero_credits_off_moves_once_to_the_soonest_reset_and_records_its_policy_cause(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=50)
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        entries = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        assert entry["kind"] == "requested"
        self.assertEqual((entry["from"], entry["to"], entry["window"], entry.get("cause")),
                         ("codex1", "codex2", "weekly", "credits off"))
        self.assertEqual(entry["why"], "codex1 weekly usage used; credits off")
        self.assertIn("codex2", self.zero_pushes()[-1].body)

    def test_zero_credits_off_without_room_switches_to_claude_on_the_same_pass_and_records_one_notice(self) -> None:
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            self.assertEqual(quota_alert.registry_assignments()["module_review"], "claude")
            for minute in (2, 30):
                _ = self.alert(self.now + timedelta(minutes=minute), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(len([entry for entry in moves.read_moves() if entry["kind"] == "no room"]), 1)
        routed = [entry for entry in moves.read_journal() if entry["kind"] == "routed to claude"]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0]["cause"], "credits off")
        self.assertEqual(set(routed[0]["moved"]), {"direct", "module_review"})
        push = self.zero_pushes()[-1]
        self.assertIn("Claude", push.body)
        self.assertIn("codex1", push.body)
        self.assertNotIn("when Codex refuses", push.body)
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_credits_on_without_another_account_names_claude_as_the_destination(self) -> None:
        _ = self.alert(spare_left=0)
        push = self.zero_pushes()[-1]
        self.assertIn("Claude", push.body)
        self.assertIn("6,370", push.body)
        self.swap.assert_not_called()

    def test_zero_failed_swap_reports_the_failure_and_switches_to_claude_on_the_same_pass(self) -> None:
        _ = self.note(1, "off")
        self.swap.side_effect = accounts.AccountsError("server could not stop")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(spare_left=50)
            fallback.assert_called_once()
            for minute in (2, 4, 30, 60):
                _ = self.alert(self.now + timedelta(minutes=minute), spare_left=50)
                self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            fallback.assert_called_once()
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        entries = moves.read_moves()
        requested = [entry for entry in entries if entry["kind"] == "requested"]
        failed = [entry for entry in entries if entry["kind"] == "failed"]
        self.assertEqual((len(requested), len(failed)), (1, 1))
        self.assertEqual(failed[0]["move_id"], requested[0]["move_id"])
        self.assertIn("server could not stop", self.zero_pushes()[-1].body)
        self.assertIn("Claude", self.zero_pushes()[-1].body)
        self.swap.assert_called_once()

    def test_credits_off_fallback_hold_survives_reload_and_blocks_recovery_until_the_earliest_refill(self) -> None:
        _ = self.note(1, "off")
        log = self.alert(spare_left=0)
        until = self.now + timedelta(days=2)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.CreditsOffHold(until))
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved and "credits_off_until" in saved["switch"]
        self.assertEqual(saved["switch"]["credits_off_until"], until.isoformat())
        self.assertFalse(any("provider refusal" in line.lower() for line in log))
        # A spare regaining room does not undo the policy fallback before its hold expires.
        for checked in (self.now + timedelta(minutes=2), until - timedelta(minutes=1)):
            _ = self.alert(checked, spare_left=50)
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            self.swap.assert_not_called()
        _ = self.alert(until, spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="Codex has room again", cause="credits off")
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request.get("cause"), "credits off")

    def test_credits_off_hold_releases_on_owner_allowance_recovery(self) -> None:
        """F001: a fresh positive owner reading releases the policy hold."""
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        _ = self.alert(self.now + timedelta(minutes=2), left=50)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_credits_off_hold_keeps_fallback_when_owner_readings_are_stale_unknown_or_missing(self) -> None:
        """F001: uncertainty about the owner cannot restore work onto a fresh spare."""
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        later = self.now + timedelta(minutes=12)
        for availability in ("stale", "unknown", "missing"):
            with self.subTest(availability=availability):
                fresh = self.readings(later, left=50)
                assert isinstance(fresh.workers, accounts.CodexWorkersAccount)
                checked: list[quota_alert.CodexHomeReading] = []
                for home in fresh.checked_homes:
                    if home.home == fresh.workers.home:
                        if availability == "missing":
                            continue
                        if availability == "unknown":
                            home.report.problem = "owner reading unavailable"
                        home = quota_alert.CodexHomeReading(home.home, home.report,
                            self.now if availability == "stale" else later)
                    checked.append(home)
                readings = quota_alert.CodexAlertPassReadings(fresh.workers, fresh.selected_account, tuple(checked))
                with mock.patch.object(self, "readings", return_value=readings):
                    _ = self.alert(later)
                self.swap.assert_not_called()
                self.assertFalse(any(entry["kind"] == "requested" for entry in moves.read_moves()))
                self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
                with quota_alert.state_file() as state:
                    self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)

    def test_failed_claude_switch_reports_work_staying_on_codex_without_internal_logs(self) -> None:
        """F006: a failed routing update cannot claim that new work moved to Claude."""
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "edit_registry", return_value="agents_set_all_assignments: permission denied"):
            _ = self.alert(spare_left=0)
        push = self.zero_pushes()[-1]
        self.assertIn("could not move", push.title)
        self.assertIn("switch to Claude failed", push.body)
        self.assertIn("permission", push.body)
        self.assertIn("Codex work stays on codex1", push.body)
        self.assertNotIn("New coding work uses Claude", push.body)
        self.assertNotIn("agents_set_all_assignments", push.body)
        self.assertNotIn("switch to claude for", push.body)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_failed_swap_names_the_account_and_user_cause_without_accounts_diagnostics(self) -> None:
        """F006: account command diagnostics belong in the journal, not on the phone."""
        _ = self.note(1, "off")
        cause = "accounts: cannot use codex2 (~/.codex): usage reading is unavailable"
        self.swap.side_effect = accounts.AccountsError(cause)
        _ = self.alert(spare_left=50)
        push = self.zero_pushes()[-1]
        self.assertIn("could not move to codex2", push.body)
        self.assertIn("its usage reading is unavailable", push.body)
        self.assertNotIn("accounts:", push.body)
        self.assertNotIn("~/.codex", push.body)
        self.assertTrue(any(entry["kind"] == "failed" and entry["reason"] == cause for entry in moves.read_moves()))

    def test_codex_catalog_examples_equal_the_notices_emitted_for_their_scenarios(self) -> None:
        """F005: reviewed example values must be the values the real alert pass emits."""
        from push_texts import catalog, render_entry
        from scripts.whoami import run_out

        self.now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        owner_resets = {1: datetime(2026, 10, 20, 4, 58, tzinfo=timezone.utc),
                        2: datetime(2026, 10, 22, 11, tzinfo=timezone.utc)}
        spare_reset = datetime(2026, 10, 17, 11, tzinfo=timezone.utc)
        base_readings = self.readings
        ids = {"quota.codex_workers_moved",
               "quota.codex_workers_stay", "quota.codex_credits_forecast"}
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/Curacao"}):
            for entry in catalog():
                if entry.id not in ids:
                    continue
                for example in entry.examples:
                    with self.subTest(id=entry.id, example=example.name):
                        quota_alert.STATE.unlink(missing_ok=True)
                        moves.store_file().unlink(missing_ok=True)
                        next_moves.next_moves_file().unlink(missing_ok=True)
                        usage_store.store_file().unlink(missing_ok=True)
                        usage_store.label_file().unlink(missing_ok=True)
                        _ = write_registry(quota_alert.REGISTRY, base_text=REGISTRY)
                        self.phone.reset_mock()
                        self.relay.reset_mock()
                        self.swap.reset_mock()
                        self.swap.side_effect = None
                        source = 2 if example.name in ("codex_two", "failed_move", "unknown_use") else 1
                        target = 3 - source
                        _ = self.note(1)
                        _ = self.note(2)
                        _ = self.note(3)
                        off = entry.id in ("quota.codex_workers_moved", "quota.codex_workers_stay")
                        _ = self.note(source, "off" if off else "on")
                        accounts.set_role("workers", f"codex{source}")
                        if example.name == "queued":
                            _ = next_moves.add_next_move("workers", 1, f"codex{source}")
                        balance = "6370" if source == 1 else "2114"
                        if example.name == "unlimited":
                            balance = "unlimited"
                        unknown_balance = example.name in ("unknown", "unknown_balance")
                        no_room = example.name in ("no_room", "unknown_use")
                        target_left = 0 if source == 2 or no_room else 97
                        target_credits = "0" if no_room else "6370" if source == 2 else "2114"
                        used = 120 if source == 1 else 40
                        if example.name == "zero_use":
                            used = 0
                        observed_minutes = 20 if example.name == "unknown_use" else 60
                        numeric_balance = 6370 if source == 1 else 2114
                        _ = run_out.READINGS_LOG.write_text("".join(json.dumps({
                            "account": f"codex{source}", "at": at.isoformat(), "remaining": 0, "credits": credits,
                        }) + "\n" for at, credits in (
                            (self.now - timedelta(minutes=observed_minutes), numeric_balance + used),
                            (self.now, numeric_balance))))

                        def scenario_readings(checked: datetime, **_ignored: object) -> quota_alert.CodexAlertPassReadings:
                            readings = base_readings(checked, selected=source, balance=None if unknown_balance else balance,
                                spare_left=0, spare_accounts=((target, target_left, target_credits),))
                            for home in readings.checked_homes:
                                number = 2 if home.home.name == ".codex" else int(home.home.name.removeprefix(".codex"))
                                reset = (owner_resets[source] if number == source else
                                         spare_reset if number == 2 else owner_resets[1]
                                         if number == 1 else owner_resets[2] + timedelta(days=1))
                                left = 0 if number == source or number == 3 else target_left
                                home.report.quotas = [Quota("Weekly", 100 - left, reset)]
                                home.report.five_hour = Quota("5-hour", 0, reset)
                            return readings

                        if example.name == "failed_move":
                            self.swap.side_effect = accounts.AccountsError(
                                "accounts: cannot use codex1 (~/.codex1): usage reading is unavailable")
                        with mock.patch.object(self, "readings", side_effect=scenario_readings):
                            if entry.id == "quota.codex_credits_forecast":
                                _ = self.alert(self.now - timedelta(hours=1))
                            _ = self.alert(self.now)
                            self.phone.side_effect = None
                        if (entry.id == "quota.codex_credits_forecast"
                                and example.name != "queued" and not no_room and target_left > 0):
                            # These old examples now move to room with no notice.
                            self.swap.assert_called()
                            self.assertEqual(self.zero_pushes(), [])
                            continue
                        with quota_alert.state_file() as state:
                            episode = state.get("codex_zero", {})[f"codex{source}"]
                            if entry.id == "quota.codex_credits_forecast":
                                assert "forecast_notice" in episode
                                notice = episode["forecast_notice"]
                            else:
                                notice = episode["notice"]
                        self.assertEqual(notice["id"], entry.id)
                        if example.name == "queued":
                            self.assertNotIn("account move you queued", " ".join(notice["values"].values()))
                        else:
                            self.assertEqual(notice["values"], example.values)
                        expected_values = dict(example.values)
                        self.assertEqual(self.zero_pushes()[-1].values, expected_values)
                        self.assertEqual(self.zero_pushes()[-1], render_entry(entry, expected_values))

    def test_credits_off_fallback_hold_uses_an_earlier_five_hour_refill(self) -> None:
        _ = self.note(1, "off")
        until = self.now + timedelta(hours=1)
        reading = self.readings(self.now, spare_left=0)
        for home in reading.checked_homes:
            home.report.five_hour = Quota("5-hour", 100, until)
        with mock.patch.object(self, "readings", return_value=reading):
            _ = self.alert(spare_left=0)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.CreditsOffHold(until))
        self.assertIn(until.strftime("%Y-%m-%d %H:%M"), self.zero_pushes()[-1].body)
        _ = self.alert(until - timedelta(minutes=1))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        self.swap.assert_not_called()
        _ = self.alert(until, spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="Codex has room again", cause="credits off")
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_credits_off_hold_releases_on_explicit_account_change(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        accounts.set_role("workers", "codex2")
        _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=50)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_credits_on_moves_to_included_room_before_another_accounts_credits(self) -> None:
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        self.assertEqual(self.swap.call_args.args, ("workers", "codex3"))
        self.phone.assert_not_called()

    def test_on_credits_notice_handles_a_destination_losing_room_during_the_pass(self) -> None:
        """F009: an account disappearing between placement reads does not abort the alert."""
        pick = accounts.pick
        chosen = False

        def room_changes(tool: str, now: datetime, *, exclude: frozenset[str], keep: accounts.NoKeep,
                         prefer_weekly_room: bool = False) -> accounts.PickedAccount | accounts.NoAccountWithRoom:
            nonlocal chosen
            self.assertEqual(tool, "codex")
            if chosen:
                return accounts.NoAccountWithRoom(accounts.EarliestReset(self.now + timedelta(days=2)), ())
            chosen = True
            first = pick("codex", now, exclude=exclude, keep=keep, prefer_weekly_room=prefer_weekly_room)
            self.assertIsInstance(first, accounts.PickedAccount)
            assert isinstance(first, accounts.PickedAccount)
            self.assertGreater(first.weekly_left, 0)
            return first

        with mock.patch.object(accounts, "pick", side_effect=room_changes) as changing_pick:
            _ = self.alert(spare_left=50)
        self.assertGreaterEqual(changing_pick.call_count, 2)
        push = self.zero_pushes()[-1]
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertIn("work moves to Claude", push.body)
        self.swap.assert_not_called()

    def test_zero_credits_off_prefers_weekly_room_over_a_sooner_reset_on_paid_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        self.swap.assert_called_once_with("workers", "codex3", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertIn("codex3", self.zero_pushes()[-1].body)
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request["to"], "codex3")

    def test_zero_credits_off_uses_a_paid_target_only_when_no_account_has_weekly_room(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 0, "0")))
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        body = self.zero_pushes()[-1].body
        self.assertIn("codex2", body)
        self.assertIn("paid credits", body)

    def test_credits_off_prefers_both_windows_with_room_over_weekly_room_funded_by_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 40, "0")), spare_five_hour=((2, 0), (3, 100)))
        self.swap.assert_called_once_with("workers", "codex3", why="codex1 weekly usage used; credits off", cause="credits off")
        body = self.zero_pushes()[-1].body
        self.assertIn("codex3", body)
        self.assertIn("40%", body)

    def test_weekly_room_with_no_five_hour_allowance_is_described_as_paid_funding(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 0, "0")), spare_five_hour=((2, 0),))
        body = self.zero_pushes()[-1].body
        self.assertIn("paid credits", body)
        self.assertNotIn("50%", body)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request.get("cause"), "credits off")

    def test_zero_reads_the_selected_accounts_setting_and_invalid_values_log_once(self) -> None:
        _ = self.note(2, "off")
        _ = self.note(1, "sometimes")
        lines = self.alert()
        self.assertEqual(lines.count("codex1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.assertIn("6,370", self.zero_pushes()[-1].body)

    def test_unknown_selected_identity_preserves_an_open_zero_episode_without_resending_it(self) -> None:
        _ = self.alert()
        with quota_alert.state_file() as state:
            original = state.get("codex_zero", {})["codex1"].copy()
        reading = self.readings(self.now + timedelta(minutes=2))
        assert isinstance(reading.workers, accounts.CodexWorkersAccount)
        unknown = quota_alert.CodexAlertPassReadings(reading.workers,
            agent_accounts.UnknownCodexAccount(reading.workers.home, "sign-in temporarily unreadable"), reading.checked_homes)
        with mock.patch.object(self, "readings", return_value=unknown):
            _ = self.alert(self.now + timedelta(minutes=2))
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"], original)
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_reading_without_a_timezone_on_its_reset_opens_no_episode(self) -> None:
        reading = self.readings(self.now)
        for home in reading.checked_homes:
            home.report.quotas = [Quota("Weekly", 100, (self.now + timedelta(days=2)).replace(tzinfo=None))]
        with mock.patch.object(self, "readings", return_value=reading):
            _ = self.alert()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {}), {})
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_zero_edited_from_credits_on_to_off_moves_once_on_the_next_pass_without_forecasts(self) -> None:
        _ = self.alert()
        opening = self.zero_pushes()[-1]
        _ = self.note(1, "off")
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        with quota_alert.state_file() as state:
            forecast_at = state.get("codex_zero", {})["codex1"]["forecast_at"]
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get("cause"), "credits off")
        for minutes in (4, 60, 120):
            _ = self.alert(self.now + timedelta(minutes=minutes))
        self.swap.assert_called_once()
        self.assertEqual(len([entry for entry in moves.read_moves() if entry["kind"] == "requested"]), 1)
        pushes = [cast(RenderedPush, call.args[0]) for call in self.phone.call_args_list]
        self.assertEqual(sum(push.id == opening.id for push in pushes), 1)
        self.assertFalse(any("forecast" in push.id for push in pushes))
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["credits"], "off")
            self.assertEqual(episode["forecast_at"], forecast_at)
            self.assertNotIn("forecast_phone", episode)

    def test_zero_edited_from_on_to_off_without_room_switches_to_claude_on_the_next_pass(self) -> None:
        _ = self.alert(spare_left=0)
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(self.now + timedelta(minutes=2), spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            _ = self.alert(self.now + timedelta(hours=1), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(len(self.zero_pushes()), 1)
        with quota_alert.state_file() as state:
            self.assertNotIn("forecast_phone", state.get("codex_zero", {})["codex1"])

    def test_credit_edit_after_workers_move_does_not_start_a_forecast_or_another_move(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=50)
        accounts.set_role("workers", "codex2")
        _ = self.note(1, "on")
        for minutes in (2, 60, 120):
            _ = self.alert(self.now + timedelta(minutes=minutes), selected=2, left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertEqual(len(self.zero_pushes()), 1)
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_invalid_credit_setting_mid_episode_still_reads_as_on(self) -> None:
        _ = self.alert()
        _ = self.note(1, "invalid")
        log = self.alert(self.now + timedelta(minutes=2))
        self.assertEqual(log.count("codex1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        self.assertEqual(len(self.zero_pushes()), 2)
        self.assertIn("forecast", self.zero_pushes()[-1].id)
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"]["credits"], "on")

    def test_zero_does_not_open_from_a_stale_selected_snapshot(self) -> None:
        readings = self.readings(self.now)
        note = self.note(1)
        _ = quota_alert.alert([note], self.now + timedelta(minutes=10, seconds=1),
            selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset()),
            codex_readings=readings)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {}), {})

    def test_zero_opening_names_unlimited_or_unknown_balance(self) -> None:
        for balance in ("unlimited", None):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                self.phone.reset_mock()
                _ = self.alert(balance=balance)
                body = self.zero_pushes()[-1].body
                if balance == "unlimited":
                    self.assertTrue("unlimited" in body or "no limit" in body)
                else:
                    self.assertIn("unknown paid credits left", body)

    def test_zero_enters_from_a_low_episode_without_restored_acknowledgement_or_repeats(self) -> None:
        _ = self.alert(left=1)
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=32))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        self.assertFalse(any("Quota restored:" in text or "/quota_ack" in text for text in self.relayed_texts()))
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])

    def test_zero_closes_silently_after_recovery_swap_away_or_weekly_reset(self) -> None:
        for close in ("recovered", "swapped", "reset"):
            with self.subTest(close=close):
                quota_alert.STATE.unlink(missing_ok=True)
                accounts.set_role("workers", "codex1")
                _ = self.alert()
                self.phone.reset_mock()
                self.relay.reset_mock()
                if close == "recovered":
                    _ = self.alert(self.now + timedelta(minutes=2), left=80)
                elif close == "swapped":
                    accounts.set_role("workers", "codex2")
                    _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=80)
                else:
                    _ = self.alert(self.now + timedelta(days=2))
                self.phone.assert_not_called()
                self.relay.assert_not_called()
                with quota_alert.state_file() as state:
                    self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_forecasts_at_an_hour_and_only_the_first_forecast_goes_to_the_phone(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.history([(0, 6370), (30, 6340), (60, 6310)])
        _ = self.alert(self.now + timedelta(minutes=58), balance="6310")
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        log = self.alert(self.now + timedelta(hours=1), balance="6310")
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        push = self.zero_pushes()[-1]
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertIn("60", push.body)
        self.assertIn("6,310", push.body)
        self.assertIn("Claude", push.body)
        self.assertEqual(self.relayed_texts(), [])
        self.history([(0, 6370), (60, 6310), (90, 6280), (120, 6250)])
        _ = self.alert(self.now + timedelta(hours=2), balance="6250")
        _ = self.alert(self.now + timedelta(hours=2, minutes=2), balance="6250")
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_first_forecast_phone_failure_retries_without_an_extra_forecast_relay(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.phone.side_effect = [quota_alert.PushFailed("offline"), quota_alert.PushSent()]
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        _ = self.alert(self.now + timedelta(hours=1, minutes=2))
        _ = self.alert(self.now + timedelta(hours=1, minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (2, 0))
        self.assertIn("no credits", self.zero_pushes()[-1].body)

    def test_zero_forecast_names_unknown_usage_when_observations_do_not_cover_the_hour(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(40, 6370), (60, 6360)])
        _ = self.alert(self.now + timedelta(hours=1), balance="6360")
        self.assertIn("unknown", self.zero_pushes()[-1].body)

    def test_zero_forecast_uses_the_observed_half_hour_for_its_run_out_rate(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(30, 120), (60, 90)])
        _ = self.alert(self.now + timedelta(hours=1), balance="90")
        text = self.zero_pushes()[-1].body
        self.assertIn("30", text)
        self.assertIn((self.now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M"), text)

    def test_zero_forecast_names_unlimited_and_unknown_current_credit_balances(self) -> None:
        for balance, expected in (("unlimited", "unlimited"), (None, "no credit balance")):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                usage_store.store_file().unlink(missing_ok=True)
                _ = self.alert()
                self.phone.reset_mock()
                _ = self.alert(self.now + timedelta(hours=1), balance=balance)
                self.assertIn(expected, self.zero_pushes()[-1].body)


if __name__ == "__main__":
    _ = unittest.main()
