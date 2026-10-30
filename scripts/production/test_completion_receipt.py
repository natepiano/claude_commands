"""A completion receipt is evidence only while its staff owner still exists."""
from __future__ import annotations

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import completion_receipt, staff
from scripts.production.staff_test_support import StaffTestCase
from production_units import Refusal


class CompletionReceiptTests(StaffTestCase):
    def test_absent_receipt_does_not_complete_a_request(self) -> None:
        result = completion_receipt.read_receipt(self.root / "absent.json", staff.initial_snapshot())
        self.assertIsInstance(result, completion_receipt.NoReceipt)

    def test_non_object_receipt_is_refused_without_rewriting_it(self) -> None:
        path = self.root / "receipt.json"
        write_text_atomic(path, '[]\n')
        before = path.read_bytes()
        with self.assertRaisesRegex(Refusal, "receipt must be an object") as refusal:
            _ = completion_receipt.read_receipt(path, staff.initial_snapshot())
        self.assertIn(str(path), str(refusal.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_retired_owner_cannot_complete_a_later_incarnations_request(self) -> None:
        path = self.root / "receipt.json"
        owner = staff.ensure("showrunner", name="tools")
        completion_receipt.write_receipt(path, owner.id, {"session_id": "old-session"})
        _ = staff.retire(owner.id)
        replacement = staff.ensure("showrunner", name="tools")
        before = path.read_bytes()
        self.assertIsInstance(completion_receipt.read_receipt(path, staff.snapshot()), completion_receipt.NoReceipt)
        self.assertNotEqual(replacement.id, owner.id)
        self.assertEqual(path.read_bytes(), before)

    def test_live_owner_recovers_saved_request_fields_after_rename(self) -> None:
        path = self.root / "nested/receipt.json"
        owner = staff.ensure("showrunner", name="tools")
        fields: dict[str, object] = {"session_id": "saved-session", "cwd": "/checkout"}
        completion_receipt.write_receipt(path, owner.id, fields)
        renamed = staff.rename(owner.id, "registry")
        result = completion_receipt.read_receipt(path, staff.snapshot())
        self.assertIsInstance(result, completion_receipt.OwnedReceipt)
        assert isinstance(result, completion_receipt.OwnedReceipt)
        self.assertEqual(result.owner, renamed)
        self.assertEqual(result.fields, {**fields, "owner": owner.id})
