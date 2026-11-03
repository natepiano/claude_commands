"""Check durable record decoding for waiting counts and delivery recovery."""
from __future__ import annotations

import json
import unittest

from scripts.production import delivery_state, staff


class DeliveryStateTests(unittest.TestCase):
    def test_a_legacy_fault_keeps_its_attempt_for_recipient_recovery(self) -> None:
        slots = delivery_state.decode(json.dumps({"attempt": "old", "state": "failed", "kind": "verdict"}))
        intent = slots.selected(True)
        self.assertEqual(intent, delivery_state.DeliveryIntent("old", "failed", "verdict"))

    def test_a_sent_validation_transfers_ownership_without_an_attempt_to_retry(self) -> None:
        slots = delivery_state.decode(json.dumps({"validation": {"attempt": "sent", "state": "sent", "feedback": "Check it"}}))
        self.assertEqual(slots.validation, delivery_state.ValidationRequested("sent", "Check it"))
        self.assertIsInstance(slots.selected(False), delivery_state.NoDelivery)

    def test_question_delivery_and_answer_records_preserve_identity_and_ownership(self) -> None:
        for record, waiting in (({"delivery": None}, True),
                                ({"delivery": {"attempt": "a", "state": "pending", "recipients": ["staff-id"]}}, False),
                                ({"delivery": {"attempt": "a", "state": "failed", "failure_note": "no showrunner to send to"}}, False)):
            with self.subTest(record=record):
                slots = delivery_state.decode(json.dumps({"question": {"question_id": "q1", "text": "Why?", **record}}))
                question = slots.question
                assert isinstance(question, delivery_state.OpenQuestion)
                self.assertEqual((question.question_id, question.text, question.awaiting_answer), ("q1", "Why?", waiting))
        answered = delivery_state.decode(json.dumps({"question": {"question_id": "q1", "text": "Why?", "answer": "Because"}}))
        self.assertEqual(answered.question, delivery_state.AnsweredQuestion("q1", "Why?", "Because"))
        self.assertIsInstance(answered.selected(False), delivery_state.NoDelivery)

    def test_corrupt_records_keep_a_saved_verdict_retryable(self) -> None:
        slots = delivery_state.decode("not json")
        self.assertEqual(slots.selected(True), delivery_state.DeliveryIntent("", "failed", "verdict"))

    def test_a_saved_verdict_selects_its_own_recipients_before_an_open_question(self) -> None:
        slots = delivery_state.decode(json.dumps({"verdict": {"attempt": "v", "state": "failed", "recipients": ["unit", "runner"]},
            "question": {"question_id": "q1", "text": "Why?", "delivery": {"attempt": "q", "state": "pending"}}}))
        self.assertEqual(slots.selected(True), delivery_state.DeliveryIntent("v", "failed", "verdict", "",
                         (staff.StaffId("unit"), staff.StaffId("runner"))))
        self.assertEqual(slots.selected(False), delivery_state.DeliveryIntent("q", "pending", "question"))
