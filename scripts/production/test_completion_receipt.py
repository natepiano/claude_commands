"""A completion receipt is evidence only while its staff owner still exists."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import completion_receipt, staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.production.production_doc import Refusal


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

    def test_missing_owner_cannot_complete_a_later_incarnations_request(self) -> None:
        path = self.root / "receipt.json"
        owner = fixture_member("showrunner", name="tools")
        completion_receipt.write_receipt(path, owner.id, {"cwd": "/old-checkout"})
        self.forget_rows(owner.id)
        replacement = fixture_member("showrunner", name="tools",
                                     runtime_id=fixture_runtime_id("replacement-tools-session"))
        before = path.read_bytes()
        self.assertIsInstance(completion_receipt.read_receipt(path, staff.snapshot()), completion_receipt.NoReceipt)
        self.assertNotEqual(replacement.id, owner.id)
        self.assertEqual(path.read_bytes(), before)

    def test_live_owner_recovers_saved_request_fields_after_rename(self) -> None:
        path = self.root / "nested/receipt.json"
        owner = fixture_member("showrunner", name="tools")
        fields: dict[str, object] = {"cwd": "/checkout"}
        completion_receipt.write_receipt(path, owner.id, fields)
        renamed = staff.rename(owner.id, "registry")
        current = staff.native_id(fixture_runtime_id("renamed-owner-session"))
        renamed = staff.update_current_session(renamed.current_session, current)
        result = completion_receipt.read_receipt(path, staff.snapshot())
        self.assertIsInstance(result, completion_receipt.OwnedReceipt)
        assert isinstance(result, completion_receipt.OwnedReceipt)
        self.assertEqual(result.owner, renamed)
        self.assertEqual(result.fields, {**fields, "owner": owner.id})

    def test_unpublished_launch_uses_its_owner_and_creation_claim(self) -> None:
        path = self.root / "launch.json"
        native = staff.native_id(fixture_runtime_id("unpublished-showrunner"))
        fields: dict[str, object] = {"doc": str(self.root / "production.md"),
                                    "config_dir": str(self.root / "account")}
        completion_receipt.write_receipt(path, native, fields)
        before = path.read_bytes()
        result = completion_receipt.read_launch_receipt(path, staff.initial_snapshot())
        self.assertIsInstance(result, completion_receipt.UnpublishedReceipt)
        assert isinstance(result, completion_receipt.UnpublishedReceipt)
        self.assertEqual(result.identity, native)
        self.assertEqual(result.fields, {**fields, "owner": native})
        self.assertIsInstance(completion_receipt.read_receipt(path, staff.initial_snapshot()),
                              completion_receipt.NoReceipt)
        self.assertEqual(path.read_bytes(), before)
