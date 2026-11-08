"""A staff hold prevents a director from starting another task."""
from __future__ import annotations

import io
import json
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from importlib import import_module
from functools import cached_property
from typing import cast, final, override
from unittest import mock

from scripts.hooks import conversation_pause
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member


@final
class StaffHoldTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = staff.pause(str(self.row.id), "review")

    @cached_property
    def row(self) -> staff.StaffRow:
        owner = fixture_member("showrunner", name="TEST-production")
        return fixture_member("unit", name="TEST-director", parent=owner.id)

    def test_a_paused_director_does_not_get_a_compaction_continuation(self) -> None:
        hook = import_module("scripts.hooks.stop-direct-continue")
        with (mock.patch.object(hook, "active_run", return_value=None) as active,
              mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": self.row.current_session})))):
            cast(Callable[[], None], hook.main)()
        active.assert_not_called()

    def test_a_peer_message_keeps_a_paused_director_at_its_prompt(self) -> None:
        with (mock.patch.object(conversation_pause, "record_lock", return_value=nullcontext()),
              mock.patch.object(conversation_pause, "_record", return_value=conversation_pause.NoPauseRecord.ABSENT),
              mock.patch.object(conversation_pause, "_pause_locked", return_value=conversation_pause.NoReply.NOTHING)):
            reply = conversation_pause.message_arrived(self.row.current_session, conversation_pause.PromptSource.PEER,
                                                       "TEST worker finished", 100)
        self.assertIsInstance(reply, conversation_pause.ContextOnly)
        assert isinstance(reply, conversation_pause.ContextOnly)
        self.assertIn("Hold at the prompt", reply.context)

    def test_the_prompt_hook_retains_the_hold_for_every_prompt_source(self) -> None:
        hook = import_module("scripts.hooks.user-prompt-submit-conversation-pause")
        payload = json.dumps({"session_id": self.row.current_session, "prompt": "TEST worker finished"})
        for source in conversation_pause.PromptSource:
            with (self.subTest(source=source),
                  mock.patch.object(conversation_pause, "prompt_source", return_value=source),
                  mock.patch.object(hook, "_record_typed_prompt"),
                  mock.patch.object(hook, "_record_activity"),
                  mock.patch("scripts.hooks.direct_run.marker_path", return_value=self.root / "missing-marker"),
                  mock.patch("signal.signal"), mock.patch("signal.alarm"),
                  mock.patch("sys.stdin", io.StringIO(payload)), redirect_stdout(io.StringIO()) as output):
                cast(Callable[[], None], hook.main)()
            self.assertTrue(output.getvalue(), source)
            result = cast(dict[str, object], json.loads(output.getvalue()))
            self.assertEqual(list(result), ["hookSpecificOutput"])
            reply = cast(dict[str, str], result["hookSpecificOutput"])
            self.assertEqual(reply["hookEventName"], "UserPromptSubmit")
            self.assertIn("Hold at the prompt", reply["additionalContext"])
            self.assertNotIn("decision", result)

        _ = staff.resume(str(self.row.id))
        with (mock.patch.object(conversation_pause, "prompt_source", return_value=conversation_pause.PromptSource.PEER),
              mock.patch.object(hook, "_record_activity"),
              mock.patch("scripts.hooks.direct_run.marker_path", return_value=self.root / "missing-marker"),
              mock.patch("signal.signal"), mock.patch("signal.alarm"),
              mock.patch("sys.stdin", io.StringIO(payload)), redirect_stdout(io.StringIO()) as output):
            cast(Callable[[], None], hook.main)()
        self.assertEqual(output.getvalue(), "")
