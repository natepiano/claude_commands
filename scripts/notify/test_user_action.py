"""Tests for the check that a push body gives no second action."""
from __future__ import annotations

import unittest

import push_texts
from user_action import ActionRequired, NoActionRequired, action_conflicts

TILE = ActionRequired("On natedev, in the tile pane, approve the prompt or add an allow rule")


class ActionConflictTests(unittest.TestCase):
    def test_every_catalog_example_gives_one_action(self) -> None:
        for entry in push_texts.catalog():
            for example in entry.examples:
                rendered = push_texts.render_entry(entry, example.values)
                with self.subTest(id=entry.id, example=example.name):
                    self.assertEqual(action_conflicts(rendered.action, rendered.body), [])

    def test_the_tile_push_gives_a_second_action_in_a_second_session(self) -> None:
        body = ("Auto mode refused 1 command in tile. It stops the tile unit's two deadlocked test runs"
                + " (process groups 3137969 and 3138422). Type ! kill -TERM -3137969 -3138422 in the cargo-liner session..")
        self.assertEqual(action_conflicts(TILE, body), [
            'the body says to type "kill -TERM -3137969 -3138422", which the Action line does not hold',
            "the body names the cargo-liner pane or session, which the Action line does not name"])

    def test_each_form_of_a_second_action(self) -> None:
        for body in ("Then type: /resume.", "Run ! ls -la now.", "Answer in the cargo-liner session.",
                     "The cargo-liner pane shows the prompt.", "In the Claude session named natedev, reply go."):
            with self.subTest(body=body):
                self.assertEqual(len(action_conflicts(TILE, body)), 1)

    def test_words_that_give_no_second_action(self) -> None:
        for body in ("You approved it.", "The pane shows the prompt.", "It does no work until you type the go.",
                     "A type of build failed.", "Done! The build works.", "The tile session waits.",
                     "The tile-unit session waits.", "a != b", "tile's session waits.", "In a Claude session, it waits."):
            with self.subTest(body=body):
                self.assertEqual(action_conflicts(TILE, body), [])

    def test_text_the_action_line_holds_is_one_action(self) -> None:
        action = ActionRequired("On natedev, in the trunk pane, type: Go: remove arrange_jacks/")
        self.assertEqual(action_conflicts(action, "Type Go: remove arrange_jacks/ in the trunk pane."), [])

    def test_an_action_with_no_pane_checks_only_what_to_type(self) -> None:
        action = ActionRequired("At chatgpt.com/codex/settings/usage, redeem your limit reset.")
        self.assertEqual(action_conflicts(action, "The cargo-liner session waits."), [])
        self.assertEqual(len(action_conflicts(action, "Then type /codex_winddown clear.")), 1)
        self.assertEqual(action_conflicts(NoActionRequired(), "Then type /codex_winddown clear."), [])


if __name__ == "__main__":
    _ = unittest.main()
