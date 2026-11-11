"""Review pauses retain their owner through production and session changes."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import review_pause, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id


class ReviewPauseTests(StaffTestCase):
    def test_pause_status_and_resume_keep_the_owner_id_after_a_rename(self) -> None:
        owner = fixture_member("showrunner", name="demo")
        state = self.root / "showrunner"
        timers = self.root / "notifier"
        instance = timers / f"showrunner-{owner.id}"
        write_text_atomic(instance / "conf", f"TARGET=session:{owner.id}\n")
        calls = self.root / "notifier-calls"
        notifier = self.root / "notifier.sh"
        write_text_atomic(notifier, '#!/bin/sh\n'
            + 'printf "%s %s\\n" "$1" "$2" >> "$REVIEW_TEST_CALLS"\n'
            + 'case "$1" in stop) enabled=0 ;; start) enabled=1 ;; esac\n'
            + 'printf "ENABLED=%s\\n" "$enabled" > "$NOTIFIER_STATE_DIR/$2/state"\n')
        notifier.chmod(0o755)
        environment = {"SHOWRUNNER_STATE_DIR": str(state), "NOTIFIER_STATE_DIR": str(timers),
                       "SHOWRUNNER_NOTIFIER": str(notifier), "REVIEW_TEST_CALLS": str(calls)}
        path = state / "review-paused" / f"{owner.id}.json"
        with patch.dict(os.environ, environment):
            for enabled, answer in ((True, "dailies"), (True, "none"), (False, "dailies")):
                with self.subTest(enabled=enabled, answer=answer):
                    _ = calls.write_text("")
                    write_text_atomic(instance / "state", f"ENABLED={int(enabled)}\n")
                    current_owner = staff.by_id(owner.id)
                    assert current_owner is not None
                    self.assertEqual(review_pause.pause(instance),
                                     f"{current_owner.name}: paused dailies" if enabled
                                     else f"{current_owner.name}: nothing to pause")
                    self.assertEqual(json.loads(path.read_text()), {"dailies": "done" if enabled else "skipped"})
                    before = path.read_bytes()
                    current = staff.native_id(fixture_runtime_id(f"review-{enabled}-{answer}"))
                    _ = staff.update_current_session(current_owner.current_session, current)
                    renamed = staff.rename(owner.id, f"renamed-{enabled}-{answer}")
                    _ = fixture_member("showrunner", name=current_owner.name,
                                       runtime_id=fixture_runtime_id(f"replacement-{enabled}-{answer}"))
                    self.assertEqual(review_pause.status(instance),
                                     f"{renamed.name}: review paused dailies" if enabled
                                     else f"{renamed.name}: review paused nothing")
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(review_pause.pause(instance), review_pause.status(instance))
                    self.assertEqual(review_pause.resume(instance, answer),
                                     f"{renamed.name}: turned on dailies" if enabled and answer == "dailies"
                                     else f"{renamed.name}: left dailies off")
                    self.assertEqual(review_pause.status(instance), "")
                    self.assertEqual(list((state / "review-paused").iterdir()), [])
                    expected = ([f"stop {instance.name}"] if enabled else [])
                    if enabled and answer == "dailies":
                        expected.append(f"start {instance.name}")
                    self.assertEqual(calls.read_text().splitlines(), expected)
                    self.assertEqual((instance / "state").read_text(),
                                     f"ENABLED={int(enabled and answer == 'dailies')}\n")
