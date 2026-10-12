"""Test the derived states and the table of pins and user answers."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, final
from zoneinfo import ZoneInfo

import pins
import upgrade_record
import upgrade_report

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        """The checker marker is an identity decorator on Python 3.11."""
        return method


NOW = datetime(2026, 10, 12, 19, 8, 41, tzinfo=timezone.utc)
REVISION = "1234567890abcdef1234567890abcdef12345678"


def entry(pin: pins.Pin, bevy: str = "0.19.1", patched: bool = False,
          via: tuple[str, ...] = ()) -> upgrade_record.Entry:
    return {"pin": pin, "bevy": bevy, "via": list(via), "patched_bevy": patched,
            "answer": None, "note": "", "crates_io": {"status": "unread"}, "error": ""}


@final
class ReportTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.current: upgrade_record.Record = {
            "slug": "demo", "production": "", "checkout": "", "target": "0.20",
            "started": NOW.isoformat(), "closed": None, "bevy": [],
            "pins_read": NOW.isoformat(), "dependencies": {},
            "target_bevy": {"status": "unread"}, "target_error": "", "checked": None,
        }

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-report-")
        self.addCleanup(folder.cleanup)
        checkout = Path(folder.name)
        _ = (checkout / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        _ = (checkout / "Cargo.lock").write_text(
            'version = 4\n[[package]]\nname = "bevy_ecs"\nversion = "0.19.1"\n'
            + 'source = "registry+https://github.com/rust-lang/crates.io-index"\n', encoding="utf-8",
        )
        doc = checkout / "demo-production.md"
        _ = doc.write_text(
            "# Demo\n\n- **Merge branch:** demo\n"
            + f"- **Showrunner checkout:** {checkout}\n"
            + "- **Log:** log.md\n- **User zone:** America/New_York\n\n## Units\n",
            encoding="utf-8",
        )
        self.current = {
            "slug": "demo", "production": str(doc), "checkout": str(checkout),
            "target": "0.20", "started": NOW.isoformat(), "closed": None,
            "bevy": ["0.19.1", "0.20.0"], "pins_read": NOW.isoformat(), "dependencies": {},
            "target_bevy": {"status": "unread"}, "target_error": "", "checked": None,
        }

    def render(self) -> str:
        return upgrade_report.table(self.current, ZoneInfo("America/New_York"), NOW)

    def test_state_order_includes_all_declared_states(self) -> None:
        self.assertEqual(upgrade_report.STATE_ORDER, (
            "git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done",
        ))

    def test_done_takes_precedence_over_a_bevy_patch(self) -> None:
        for patched in (False, True):
            with self.subTest(patched=patched):
                self.assertEqual(upgrade_report.state(entry({"source": "registry", "version": "1.0.0"},
                                                    "0.20.3", patched), "0.20"), "done")

    def test_a_patched_bevy_crate_requires_revendoring(self) -> None:
        current = entry({"source": "path", "version": "0.19.1", "path": "vendor/bevy"}, patched=True)
        self.assertEqual(upgrade_report.state(current, "0.20"), "re-vendor")

    def test_answers_and_notes_do_not_change_not_checked(self) -> None:
        current = entry({"source": "git", "version": "1.0.0", "url": "https://github.com/example/adapter",
                         "ref": "branch=main", "rev": REVISION})
        current["answer"] = {"answer": "approved", "at": "2026-10-12", "words": "use this git pin"}
        current["note"] = "a port may exist"
        self.assertEqual(upgrade_report.state(current, "0.20"), "not checked")

    def test_header_prefix_and_blank_line(self) -> None:
        lines = self.render().splitlines()
        self.assertEqual(lines[:4], [
            f"Bevy 0.20 upgrade for demo: {self.current['checkout']}, on Bevy 0.19.1 and 0.20.0.",
            "Bevy 0.20: release not looked up yet. Upstream: no complete check yet.", "",
            "| Dependency | Our pin | crates.io | Git | State | Next |",
        ])
        self.assertNotIn("Answers on git pins:", lines)
        self.assertNotIn("Notes:", lines)

    def test_rows_sort_by_state_then_name_with_exact_cells(self) -> None:
        self.current["dependencies"] = {
            "a-done": entry({"source": "registry", "version": "5.0.0"}, "0.20.0"),
            "z-unchecked": entry({"source": "registry", "version": "2.0.0"}),
            "a-unchecked": entry({"source": "registry", "version": "3.0.0"}, via=("a-parent", "z-parent")),
            "z-patched": entry({"source": "path", "version": "0.19.1", "path": "vendor/z"}, patched=True),
            "a-patched": entry({"source": "path", "version": "0.19.1", "path": "../vendor/a"}, patched=True),
        }
        lines = self.render().splitlines()
        rows = [line for line in lines if line.startswith(("| a-", "| z-"))]
        self.assertEqual(rows, [
            "| a-patched (patched Bevy crate) | path ../vendor/a, 0.19.1 | — | — | re-vendor | re-vendor from Bevy 0.20 |",
            "| z-patched (patched Bevy crate) | path vendor/z, 0.19.1 | — | — | re-vendor | re-vendor from Bevy 0.20 |",
            "| a-unchecked (via a-parent, z-parent) | 3.0.0 on Bevy 0.19.1 | not checked | — | not checked | run check |",
            "| z-unchecked | 2.0.0 on Bevy 0.19.1 | not checked | — | not checked | run check |",
            "| a-done | 5.0.0 on Bevy 0.20.0 | — | — | done | — |",
        ])

    def test_git_pin_prints_its_ref_or_short_revision(self) -> None:
        for ref, label in (("branch=main", "branch=main"), ("rev=abc1234", "rev=abc1234"),
                           ("tag=v1", "tag=v1"), ("", REVISION[:7])):
            with self.subTest(ref=ref):
                self.current["dependencies"] = {"adapter": entry({
                    "source": "git", "version": "1.0.0", "url": "https://github.com/example/adapter",
                    "ref": ref, "rev": REVISION,
                })}
                self.assertIn(f"| adapter | git {label} @ {REVISION[:7]} on Bevy 0.19.1 | not checked | — | not checked | run check |",
                              self.render().splitlines())

    def test_a_path_dependent_prints_its_bevy_version(self) -> None:
        self.current["dependencies"] = {
            "adapter": entry({"source": "path", "version": "2.0.0", "path": "vendor/adapter"}),
        }
        self.assertIn("| adapter | path vendor/adapter, 2.0.0 on Bevy 0.19.1 | — | — | re-vendor | port the copy at vendor/adapter to Bevy 0.20 |",
                      self.render().splitlines())

    def test_patched_bevy_label_and_pin_omit_via_and_bevy_suffix(self) -> None:
        self.current["dependencies"] = {"bevy_render": entry({
            "source": "git", "version": "0.20.0", "url": "https://github.com/example/bevy",
            "ref": "branch=port", "rev": REVISION,
        }, "0.20.0", True, ("wrapper",))}
        self.assertIn(f"| bevy_render (patched Bevy crate) | git branch=port @ {REVISION[:7]} | — | — | done | — |",
                      self.render().splitlines())

    def test_answers_and_notes_follow_the_rows_and_keep_the_users_words(self) -> None:
        git_entry = entry({"source": "git", "version": "1.0.0", "url": "https://github.com/example/git",
                           "ref": "", "rev": REVISION})
        git_entry["answer"] = {"answer": "approved", "at": "2026-10-12", "words": "yes, use this fork"}
        git_entry["note"] = "CI needs feature x"
        registry_entry = entry({"source": "registry", "version": "2.0.0"})
        registry_entry["answer"] = {"answer": "declined", "at": "2026-10-13", "words": "stay with this release"}
        registry_entry["note"] = "  my exact reason  "
        self.current["dependencies"] = {"git-adapter": git_entry, "registry-adapter": registry_entry}
        lines = self.render().splitlines()
        answers_at = lines.index("Answers on git pins:")
        notes_at = lines.index("Notes:")
        self.assertGreater(answers_at, max(index for index, line in enumerate(lines) if line.startswith("| ")))
        self.assertGreater(notes_at, answers_at)
        self.assertIn('- git-adapter: approved 2026-10-12: "yes, use this fork"', lines[answers_at:notes_at])
        self.assertIn('- registry-adapter: declined 2026-10-13: "stay with this release"', lines[answers_at:notes_at])
        self.assertIn("- git-adapter: CI needs feature x", lines[notes_at:])
        self.assertIn("- registry-adapter:   my exact reason  ", lines[notes_at:])

    def test_an_empty_answer_and_note_add_no_sections(self) -> None:
        self.current["dependencies"] = {"adapter": entry({"source": "registry", "version": "1.0.0"})}
        output = self.render()
        self.assertNotIn("Answers on git pins:", output)
        self.assertNotIn("Notes:", output)

    def checked_entry(self, newest: str = "2.0.0", ready: str = "") -> upgrade_record.Entry:
        current = entry({"source": "registry", "version": "1.0.0"})
        current["crates_io"] = {"status": "read", "checked": NOW.isoformat(), "newest": newest,
                                "ready": ready, "repository": "owner/adapter",
                                "releases": {"3.0.0": "", "2.0.0": "^0.19", "1.0.0": "^0.20"}}
        return current

    def test_done_unread_cell_is_empty_while_read_facts_remain_visible(self) -> None:
        unread = entry({"source": "registry", "version": "5.0.0"}, "0.20.0")
        read = self.checked_entry()
        read["bevy"] = "0.20.0"
        self.current["dependencies"] = {"done-unread": unread, "done-read": read,
                                        "waiting": entry({"source": "registry", "version": "1.0.0"})}
        lines = self.render().splitlines()
        self.assertIn("| done-unread | 5.0.0 on Bevy 0.20.0 | — | — | done | — |", lines)
        self.assertIn("| done-read | 1.0.0 on Bevy 0.20.0 | 2.0.0 on Bevy ^0.19 | — | done | — |", lines)
        self.assertIn("| waiting | 1.0.0 on Bevy 0.19.1 | not checked | — | not checked | run check |", lines)

    def test_ready_and_no_port_states_and_next_steps(self) -> None:
        self.current["dependencies"] = {"ready": self.checked_entry(ready="1.0.0"), "waiting": self.checked_entry()}
        lines = self.render().splitlines()
        self.assertIn("| ready | 1.0.0 on Bevy 0.19.1 | 2.0.0 on Bevy ^0.19 → 1.0.0 on Bevy ^0.20 | — | release ready | bump to 1.0.0 |", lines)
        self.assertIn("| waiting | 1.0.0 on Bevy 0.19.1 | 2.0.0 on Bevy ^0.19 | — | no port yet | wait |", lines)
        self.assertLess(lines.index(next(line for line in lines if line.startswith("| ready |"))),
                        lines.index(next(line for line in lines if line.startswith("| waiting |"))))

    def test_ready_equal_to_newest_does_not_repeat_it_and_unknown_requirement_is_explicit(self) -> None:
        current = self.checked_entry("1.0.0", "1.0.0")
        self.current["dependencies"] = {"adapter": current}
        text = self.render()
        self.assertIn("| 1.0.0 on Bevy ^0.20 |", text)
        self.assertNotIn(" → ", text)
        current["crates_io"] = {"status": "read", "checked": NOW.isoformat(), "newest": "3.0.0", "ready": "1.0.0",
                                "repository": "", "releases": {"3.0.0": "", "1.0.0": "^0.20"}}
        self.assertIn("3.0.0 requirement unknown → 1.0.0 on Bevy ^0.20", self.render())

    def test_pin_and_done_states_take_precedence_over_cached_ready(self) -> None:
        current = self.checked_entry(ready="1.0.0")
        current["pin"] = {"source": "path", "version": "1.0.0", "path": "vendor/adapter"}
        self.assertEqual(upgrade_report.state(current, "0.20"), "re-vendor")
        current["bevy"] = "0.20.1"
        self.assertEqual(upgrade_report.state(current, "0.20"), "done")

    def test_entry_error_appends_to_unread_read_and_path_cells(self) -> None:
        self.current["dependencies"] = {"unread": entry({"source": "registry", "version": "1.0.0"}),
                                        "read": self.checked_entry(),
                                        "path": entry({"source": "path", "version": "1.0.0", "path": "vendor/adapter"})}
        for value in self.current["dependencies"].values():
            value["error"] = "crates.io: HTTP 503"
        text = self.render()
        for cell in ("not checked", "2.0.0 on Bevy ^0.19", "—"):
            self.assertIn(f"| {cell}; last check failed: crates.io: HTTP 503 |", text)

    def test_second_line_for_each_target_status(self) -> None:
        cases: tuple[tuple[upgrade_record.TargetBevy, str], ...] = (
            ({"status": "unread"}, "release not looked up yet"),
            ({"status": "unpublished"}, "not released yet"),
            ({"status": "pre-release", "since": "2026-10-01"}, "not released yet"),
            ({"status": "released", "version": "0.20.2", "date": "2026-10-12", "since": "2026-10-01"},
             "released as 0.20.2 on 2026-10-12"),
        )
        for target, words in cases:
            with self.subTest(target=target):
                self.current["target_bevy"] = target
                self.assertEqual(self.render().splitlines()[1], f"Bevy 0.20: {words}. Upstream: no complete check yet.")

    def test_second_line_fresh_and_stale_checks_use_production_zone(self) -> None:
        self.current["checked"] = NOW.isoformat()
        self.assertEqual(self.render().splitlines()[1],
                         "Bevy 0.20: release not looked up yet. Upstream checked 15:08 EDT.")
        self.current["checked"] = (NOW - timedelta(hours=8)).isoformat()
        self.assertNotIn("stale", self.render().splitlines()[1])
        self.current["checked"] = (NOW - timedelta(hours=8, seconds=1)).isoformat()
        self.assertEqual(self.render().splitlines()[1],
                         "Bevy 0.20: release not looked up yet. Upstream last checked 2026-10-12 07:08 EDT (stale).")
        self.current["checked"] = NOW.isoformat()
        self.current["dependencies"] = {"adapter": self.checked_entry()}
        self.current["dependencies"]["adapter"]["error"] = "crates.io: offline"
        self.assertIn("Upstream last checked 2026-10-12 15:08 EDT (stale).", self.render().splitlines()[1])

    def test_failed_target_lookup_is_printed_and_marks_checked_data_stale(self) -> None:
        self.current["target_bevy"] = {"status": "pre-release", "since": "2026-10-01"}
        self.current["target_error"] = "crates.io: HTTP 503"
        self.assertEqual(self.render().splitlines()[1],
                         "Bevy 0.20: not released yet. Release lookup failed: crates.io: HTTP 503. Upstream: no complete check yet.")
        self.current["checked"] = NOW.isoformat()
        self.assertEqual(self.render().splitlines()[1],
                         "Bevy 0.20: not released yet. Release lookup failed: crates.io: HTTP 503. Upstream last checked 2026-10-12 15:08 EDT (stale).")


if __name__ == "__main__":
    _ = unittest.main()
