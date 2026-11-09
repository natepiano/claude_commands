"""Decision priority follows explicit deadlines and the current configuration."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.notify import decision_priority
from scripts.notify import silence
from scripts.notify import pushover
from scripts.notify import push_texts
from scripts.notify import review
from scripts.notify.user_action import ActionRequired


class DecisionPriorityTests(unittest.TestCase):
    root: Path = Path()
    now: datetime = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root)})))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.root / "notify")})))
        _ = self.enterContext(patch.object(pushover, "log"))
        self.configure(1800)

    def configure(self, seconds: int) -> None:
        _ = (self.root / "send.conf").write_text(
            f"# range: 0.001..\ndecision_high_priority_window_seconds={seconds}\n")

    def test_decision_deadline_window_includes_its_upper_boundary_only(self) -> None:
        for offset, expected in ((3600, "0"), (1800, "1"), (1, "1"), (0, "0"), (-1, "0")):
            with self.subTest(offset=offset), redirect_stderr(io.StringIO()):
                due = (self.now + timedelta(seconds=offset)).isoformat()
                self.assertEqual(decision_priority.priority("decision", due, now=self.now), expected)
        self.assertEqual(decision_priority.priority("decision", now=self.now), "0")
        self.assertEqual(decision_priority.priority("blocked", now=self.now), "2")
        self.assertEqual(decision_priority.priority("note", now=self.now), "0")

    def test_deadlines_reject_missing_offsets_and_malformed_dates(self) -> None:
        for value in ("tomorrow", "2026-11-03T12:15:00", "2026-99-03T12:15:00+00:00"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "explicit offset"):
                _ = decision_priority.read_due(value)
            self.assertIsNone(pushover.parse(["--priority", "1", "--due", value, "--action", "Reply", "Title", "Body"]))

    def test_changed_window_changes_priority_without_a_reload(self) -> None:
        due = (self.now + timedelta(seconds=1200)).isoformat()
        self.assertEqual(decision_priority.priority("decision", due, now=self.now), "1")
        self.configure(600)
        self.assertEqual(decision_priority.priority("decision", due, now=self.now), "0")

    def test_priority_override_and_rendered_decision_require_an_in_window_due(self) -> None:
        entry = push_texts.PushText("test.decision", "owner", "decision", "Choose a step",
            push_texts.ActionTemplate("Reply yes or no"), "Choose the next step.",
            (push_texts.PushExample("typical", {}),), purpose="Test decision priority.")
        with patch.object(push_texts, "catalog", return_value=(entry,)):
            for template in (False, True):
                for seconds, expected in ((None, "0"), (900, "1"), (3600, "0")):
                    with self.subTest(template=template, seconds=seconds):
                        args = ["--priority", "1"]
                        if seconds is not None:
                            args += ["--due", (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()]
                        args += ["--text-id", entry.id] if template else ["--action", "Reply", "Title", "Body"]
                        found = pushover.parse(args)
                        assert found is not None
                        self.assertEqual(found["priority"], expected)

    def test_kept_release_recomputes_priority_and_preserves_the_deadline(self) -> None:
        due = (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
        push: silence.KeptPush = {"source": "owner", "priority": "1", "title": "Title", "message": "Body",
                "detail": "", "key": "question", "text_id": "", "need": "decision", "due": due}
        with patch.object(pushover, "post_message", return_value=0) as post:
            self.assertEqual(pushover.post_kept(push), 0)
            self.assertEqual(post.call_args.args[1], "1")
            self.configure(60)
            self.assertEqual(pushover.post_kept(push), 0)
            self.assertEqual(post.call_args.args[1], "0")

    def test_review_hold_serialization_preserves_the_due_time(self) -> None:
        due = (self.now + timedelta(seconds=900)).isoformat()
        draft = review.Draft("owner", "1", "Choose", ActionRequired("Reply"), "The choice", False, due=due)
        stored = review.draft_record(draft)
        self.assertEqual(stored.get("due"), due)
        with patch.object(review, "schedule"):
            review.hold(review.Held("owner", self.now.timestamp(), draft, draft, ["Retry?"]))
        found = review.held_for("owner")
        assert isinstance(found, review.LiveHold)
        self.assertEqual(found.hold.draft.due, due)
        with patch.object(silence, "keep_here", return_value=silence.Kept("until off")) as keep:
            self.assertEqual(pushover.deliver(found.hold.best_version), 0)
        kept = cast(silence.KeptPush, keep.call_args.args[0])
        self.assertEqual(kept.get("due"), due)

    def test_kept_overdue_decision_logs_and_returns_to_normal_priority(self) -> None:
        push: silence.KeptPush = {"source": "owner", "priority": "1", "title": "Title", "message": "Body",
                "detail": "", "key": "question", "text_id": "", "need": "decision",
                "due": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        with patch.object(pushover, "post_message", return_value=0) as post, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(pushover.post_kept(push), 0)
        self.assertEqual(post.call_args.args[1], "0")
        self.assertIn("overdue deadline", errors.getvalue())
