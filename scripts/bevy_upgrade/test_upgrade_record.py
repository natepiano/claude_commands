"""Keep pins, answers and notes in a temporary, exclusively locked upgrade_record."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from unittest import mock
from zoneinfo import ZoneInfo

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

import production_units
import pins
import upgrade_record

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        """The checker marker is an identity decorator on Python 3.11."""
        return method


NOW = datetime(2026, 10, 12, 19, 8, 41, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 13, 1, 2, 3, tzinfo=timezone.utc)


def dependent(name: str, version: str = "1.0.0") -> pins.Dependent:
    return pins.Dependent(name, {"source": "registry", "version": version}, "0.19.1", (), False)


@final
class RecordTests(unittest.TestCase):
    root: Path = Path()
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.production = production_units.Production(Path(), "demo", "demo", Path(), Path(), ZoneInfo("UTC"))
        self.current_pins = pins.Pins((), frozenset(), ())

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-record-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        checkout = self.root / "checkout"
        checkout.mkdir()
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
        self.production = production_units.read_production(doc)
        self.current_pins = pins.Pins(("0.19.1",), frozenset({"bevy_ecs"}),
                                      (dependent("alpha"), dependent("zeta")))
        _ = cast(object, self.enterContext(mock.patch.dict(
            os.environ, {"BEVY_UPGRADE_STATE_DIR": str(self.root / "state")},
        )))

    def begin(self) -> upgrade_record.Record:
        with upgrade_record.locked("demo"):
            started = upgrade_record.start(self.production, "0.20", self.current_pins, NOW)
            upgrade_record.save(started)
        return started

    def test_state_override_and_record_path(self) -> None:
        self.assertEqual(upgrade_record.state_dir(), self.root / "state")
        self.assertEqual(upgrade_record.record_path("demo"), self.root / "state/demo.json")

    def test_default_state_directory_uses_the_home(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "home")}, clear=True):
            self.assertEqual(upgrade_record.state_dir(), self.root / "home/.local/state/bevy-upgrade")

    def test_missing_record_loads_as_none(self) -> None:
        with upgrade_record.locked("demo"):
            self.assertIsNone(upgrade_record.load("demo"))

    def test_unique_module_names_load_with_shutdown_first_on_the_path(self) -> None:
        directory = Path(__file__).resolve().parent
        shutdown = directory.parent / "shutdown"
        script = (
            "import sys\nfrom pathlib import Path\n"
            + "sys.path.insert(0, sys.argv[1])\nsys.path.insert(0, sys.argv[2])\n"
            + "import shutdown_record\nimport upgrade_record\nimport upgrade_report\n"
            + "assert Path(shutdown_record.__file__).resolve() == Path(sys.argv[2]) / 'shutdown_record.py'\n"
            + "assert Path(upgrade_record.__file__).resolve() == Path(sys.argv[1]) / 'upgrade_record.py'\n"
            + "assert Path(upgrade_report.__file__).resolve() == Path(sys.argv[1]) / 'upgrade_report.py'\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(directory), str(shutdown)],
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_start_builds_the_specified_record_without_default_answers(self) -> None:
        started = self.begin()
        self.assertEqual(started, {
            "slug": "demo", "production": str(self.production.doc), "checkout": str(self.production.checkout),
            "target": "0.20", "started": NOW.isoformat(), "closed": None,
            "bevy": ["0.19.1"], "pins_read": NOW.isoformat(),
            "target_bevy": {"status": "unread"}, "target_error": "", "checked": None, "announced": None,
            "dependencies": {name: {
                "pin": {"source": "registry", "version": "1.0.0"}, "bevy": "0.19.1", "via": [],
                "patched_bevy": False, "answer": None, "note": "",
                "crates_io": {"status": "unread"}, "error": "",
                "branch": {"status": "unread"}, "items": [],
            } for name in ("alpha", "zeta")},
        })

    def test_record_round_trips_through_json(self) -> None:
        started = self.begin()
        with upgrade_record.locked("demo"):
            self.assertEqual(upgrade_record.load("demo"), started)
        raw: object = json.loads(upgrade_record.record_path("demo").read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        self.assertEqual(raw, started)
        self.assertEqual(list(upgrade_record.state_dir().glob("*.json")), [upgrade_record.record_path("demo")])

    def test_unreadable_record_refuses_with_its_path(self) -> None:
        for content in (b"{bad json", b"\xff"):
            with self.subTest(content=content), upgrade_record.locked("demo"):
                path = upgrade_record.record_path("demo")
                _ = path.write_bytes(content)
                with self.assertRaises(upgrade_record.RecordFailure) as caught:
                    _ = upgrade_record.load("demo")
                self.assertIn(str(path), str(caught.exception))

    def test_locked_holds_an_exclusive_flock_and_releases_it(self) -> None:
        with upgrade_record.locked("demo"):
            lock_path = upgrade_record.state_dir() / "demo.lock"
            self.assertTrue(lock_path.is_file())
            with lock_path.open("a") as competitor:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with lock_path.open("a") as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(competitor, fcntl.LOCK_UN)

    def test_failed_atomic_replace_keeps_the_previous_record(self) -> None:
        started = self.begin()
        old_contents = upgrade_record.record_path("demo").read_bytes()
        updated = upgrade_record.set_note(started, "alpha", "new note")
        with upgrade_record.locked("demo"), mock.patch("os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises((OSError, upgrade_record.RecordFailure)):
                upgrade_record.save(updated)
        self.assertEqual(upgrade_record.record_path("demo").read_bytes(), old_contents)

    def test_locked_propagates_the_callers_oserror_and_releases_the_lock(self) -> None:
        failure = OSError("caller operation failed")
        with self.assertRaises(OSError) as caught:
            with upgrade_record.locked("demo"):
                raise failure
        self.assertIs(caught.exception, failure)
        with (upgrade_record.state_dir() / "demo.lock").open("a") as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(competitor, fcntl.LOCK_UN)

    def test_locked_wraps_open_and_flock_failures_with_the_lock_path(self) -> None:
        for operation in ("pathlib.Path.open", "fcntl.flock"):
            with self.subTest(operation=operation), mock.patch(operation, side_effect=OSError("lock failed")):
                with self.assertRaises(upgrade_record.RecordFailure) as caught:
                    with upgrade_record.locked("demo"):
                        self.fail("lock acquisition should have failed")
            self.assertEqual(str(caught.exception), f"{self.root / 'state/demo.lock'}: lock failed")

    def test_same_target_start_refreshes_pins_and_keeps_answers_and_notes(self) -> None:
        started = self.begin()
        with upgrade_record.locked("demo"):
            started = upgrade_record.set_answer(started, "alpha", "approved", "use this crate", "2026-10-12")
            started = upgrade_record.set_note(started, "alpha", "keep the local fix")
            upgrade_record.save(started)
            updated_pins = pins.Pins(("0.19.2",), frozenset({"bevy_ecs"}), (dependent("alpha", "2.0.0"),))
            again = upgrade_record.start(self.production, "0.20", updated_pins, LATER)
            upgrade_record.save(again)
        self.assertEqual(again["started"], started["started"])
        self.assertEqual(again["pins_read"], LATER.isoformat())
        self.assertEqual(again["bevy"], ["0.19.2"])
        self.assertEqual(again["dependencies"]["alpha"]["pin"], {"source": "registry", "version": "2.0.0"})
        self.assertEqual(again["dependencies"]["alpha"]["answer"], started["dependencies"]["alpha"]["answer"])
        self.assertEqual(again["dependencies"]["alpha"]["note"], "keep the local fix")
        self.assertNotIn("zeta", again["dependencies"])

    def test_a_second_open_target_is_refused_without_changing_the_record(self) -> None:
        started = self.begin()
        with upgrade_record.locked("demo"):
            with self.assertRaises(upgrade_record.RecordFailure) as caught:
                _ = upgrade_record.start(self.production, "0.21", self.current_pins, LATER)
            self.assertEqual(str(caught.exception), "a watch for 0.20 is open; stop it first")
            self.assertEqual(upgrade_record.load("demo"), started)

    def test_start_refuses_targets_other_than_major_minor(self) -> None:
        for target in ("", "0", "0.20.0", "v0.20", "0.20-rc.1", " 0.20", "0.20\n"):
            with self.subTest(target=target), upgrade_record.locked("demo"):
                with self.assertRaises(upgrade_record.RecordFailure) as caught:
                    _ = upgrade_record.start(self.production, target, self.current_pins, NOW)
                self.assertEqual(str(caught.exception), "target must be major.minor, such as 0.20")
                self.assertIsNone(upgrade_record.load("demo"))

    def test_start_archives_a_closed_watch_and_resets_answers_and_notes(self) -> None:
        old = self.begin()
        with upgrade_record.locked("demo"):
            old = upgrade_record.set_answer(old, "alpha", "declined", "stay on releases", "2026-10-12")
            old = upgrade_record.set_note(old, "alpha", "old upgrade note")
            old["closed"] = LATER.isoformat()
            upgrade_record.save(old)
            fresh = upgrade_record.start(self.production, "0.21", self.current_pins, LATER)
            upgrade_record.save(fresh)
        archive = upgrade_record.state_dir() / "demo.0.20.closed.json"
        archived: object = json.loads(archive.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        self.assertEqual(archived, old)
        self.assertEqual(fresh["target"], "0.21")
        self.assertEqual(fresh["started"], LATER.isoformat())
        self.assertIsNone(fresh["closed"])
        self.assertIsNone(fresh["dependencies"]["alpha"]["answer"])
        self.assertEqual(fresh["dependencies"]["alpha"]["note"], "")

    def test_apply_pins_refreshes_fields_removes_old_rows_and_keeps_user_data(self) -> None:
        current = self.begin()
        current = upgrade_record.set_answer(current, "alpha", "approved", "  yes, keep my words\nexactly  ", "2026-10-12")
        current = upgrade_record.set_note(current, "alpha", "CI needs a local feature")
        old_answer = current["dependencies"]["alpha"]["answer"]
        next_pins = pins.Pins(("0.19.2", "0.20.0"), frozenset({"bevy_ecs", "bevy_render"}), (
            pins.Dependent("alpha", {"source": "path", "version": "2.0.0", "path": "vendor/alpha"},
                           "0.19.2", ("wrapper",), True),
            dependent("beta"),
        ))
        merged = upgrade_record.apply_pins(current, next_pins, LATER)
        self.assertEqual(set(merged["dependencies"]), {"alpha", "beta"})
        self.assertEqual(merged["dependencies"]["alpha"], {
            "pin": {"source": "path", "version": "2.0.0", "path": "vendor/alpha"},
            "bevy": "0.19.2", "via": ["wrapper"], "patched_bevy": True,
            "answer": old_answer, "note": "CI needs a local feature",
            "crates_io": {"status": "unread"}, "error": "",
            "branch": {"status": "unread"}, "items": [],
        })
        self.assertIsNone(merged["dependencies"]["beta"]["answer"])
        self.assertEqual(merged["dependencies"]["beta"]["note"], "")
        self.assertEqual(merged["bevy"], ["0.19.2", "0.20.0"])
        self.assertEqual(merged["pins_read"], LATER.isoformat())
        self.assertEqual(merged["started"], NOW.isoformat())

    def test_an_answer_is_verbatim_replaced_and_forgotten_for_one_crate(self) -> None:
        current = self.begin()
        words = '  use this fork "as is"\nwith the patch  '
        current = upgrade_record.set_note(current, "alpha", "leave this note")
        current = upgrade_record.set_answer(current, "alpha", "approved", words, "2026-10-12")
        self.assertEqual(current["dependencies"]["alpha"]["answer"], {
            "answer": "approved", "at": "2026-10-12", "words": words,
        })
        self.assertIsNone(current["dependencies"]["zeta"]["answer"])
        current = upgrade_record.set_answer(current, "alpha", "declined", "changed my mind", "2026-10-13")
        self.assertEqual(current["dependencies"]["alpha"]["answer"], {
            "answer": "declined", "at": "2026-10-13", "words": "changed my mind",
        })
        current = upgrade_record.forget_answer(current, "alpha")
        self.assertIsNone(current["dependencies"]["alpha"]["answer"])
        self.assertEqual(current["dependencies"]["alpha"]["note"], "leave this note")
        current = upgrade_record.forget_answer(current, "alpha")
        self.assertIsNone(current["dependencies"]["alpha"]["answer"])

    def test_empty_answer_words_are_refused(self) -> None:
        current = self.begin()
        with self.assertRaises(upgrade_record.RecordFailure):
            _ = upgrade_record.set_answer(current, "alpha", "approved", "", "2026-10-12")
        self.assertIsNone(current["dependencies"]["alpha"]["answer"])

    def test_unknown_crates_are_refused_by_all_three_operations(self) -> None:
        current = self.begin()
        for operation in ("answer", "forget", "note"):
            with self.subTest(operation=operation), self.assertRaises(upgrade_record.RecordFailure) as caught:
                if operation == "answer":
                    _ = upgrade_record.set_answer(current, "missing", "approved", "yes", "2026-10-12")
                elif operation == "forget":
                    _ = upgrade_record.forget_answer(current, "missing")
                else:
                    _ = upgrade_record.set_note(current, "missing", "why")
            self.assertEqual(str(caught.exception), "missing is not tracked; tracked: alpha, zeta")

    def test_notes_can_be_replaced_and_cleared_without_changing_an_answer(self) -> None:
        current = self.begin()
        current = upgrade_record.set_answer(current, "alpha", "approved", "use git", "2026-10-12")
        answer = current["dependencies"]["alpha"]["answer"]
        for note in ("  verbatim note\nline two  ", "new reason", ""):
            current = upgrade_record.set_note(current, "alpha", note)
            self.assertEqual(current["dependencies"]["alpha"]["note"], note)
            self.assertEqual(current["dependencies"]["alpha"]["answer"], answer)

    def test_on_target_compares_the_major_and_minor_components(self) -> None:
        for version in ("0.20.0", "0.20.17", "0.20.0-rc.1", "0.20.0+local"):
            with self.subTest(version=version):
                self.assertTrue(upgrade_record.on_target(version, "0.20"))
        for version in ("0.2.0", "0.19.9", "0.200.0", "1.20.0"):
            with self.subTest(version=version):
                self.assertFalse(upgrade_record.on_target(version, "0.20"))

    def test_target_fit_accepts_simple_versions_only(self) -> None:
        for requirement in ("0.20", "0.20.1", "^0.20", "~0.20.3", "=0.20.0", "0.20.0-rc.1"):
            with self.subTest(requirement=requirement):
                self.assertEqual(upgrade_record.target_fit(requirement, "0.20"), "on target")
        for requirement in ("^0.19", "0.200.0", "1.20.0", "~0.21.0-rc.1"):
            with self.subTest(requirement=requirement):
                self.assertEqual(upgrade_record.target_fit(requirement, "0.20"), "off target")
        for requirement in ("", "*", ">=0.20", "^0.20, <0.21", "0.20.*", "^^0.20", "0.20.0+local", " 0.20"):
            with self.subTest(requirement=requirement):
                self.assertEqual(upgrade_record.target_fit(requirement, "0.20"), "unreadable")

    def test_load_migrates_phase_one_record_with_starting_values(self) -> None:
        current = self.begin()
        raw = cast(dict[str, object], json.loads(json.dumps(current)))
        for key in ("target_bevy", "target_error", "checked"):
            _ = raw.pop(key)
        dependencies = cast(dict[str, dict[str, object]], raw["dependencies"])
        for value in dependencies.values():
            _ = value.pop("crates_io")
            _ = value.pop("error")
        with upgrade_record.locked("demo"):
            _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
            loaded = upgrade_record.load("demo")
        self.assertEqual(loaded, current)

    def test_valid_added_fields_round_trip_for_every_target_variant(self) -> None:
        current = self.begin()
        targets: tuple[upgrade_record.TargetBevy, ...] = (
            {"status": "unread"}, {"status": "unpublished"},
            {"status": "pre-release", "since": "2026-10-01"},
            {"status": "released", "version": "0.20.0", "date": "2026-10-12", "since": "2026-10-01"},
        )
        current["checked"] = NOW.isoformat()
        current["target_error"] = "crates.io: old failure"
        current["dependencies"]["alpha"]["crates_io"] = {
            "status": "read", "checked": NOW.isoformat(), "releases": {"2.0.0": "^0.20", "1.0.0": ""},
            "newest": "2.0.0", "ready": "2.0.0", "repository": "owner/adapter",
        }
        current["dependencies"]["alpha"]["error"] = "crates.io: HTTP 503"
        for target in targets:
            with self.subTest(target=target), upgrade_record.locked("demo"):
                current["target_bevy"] = target
                upgrade_record.save(current)
                self.assertEqual(upgrade_record.load("demo"), current)

    def test_load_validates_each_added_record_field_and_target_variant(self) -> None:
        current = self.begin()
        cases: tuple[tuple[str, object], ...] = (
            ("checked", 1), ("checked", "not-a-time"), ("target_error", None),
            ("target_bevy", None), ("target_bevy", {"status": "unknown"}),
            ("target_bevy", {"status": "pre-release", "since": 1}),
            ("target_bevy", {"status": "released", "version": "0.20.0", "date": "2026-10-12"}),
            ("target_bevy", {"status": "released", "version": 1, "date": "2026-10-12", "since": "2026-10-01"}),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), upgrade_record.locked("demo"):
                raw = cast(dict[str, object], json.loads(json.dumps(current)))
                raw[field] = value
                _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(upgrade_record.RecordFailure):
                    _ = upgrade_record.load("demo")

    def test_load_validates_each_added_entry_field_and_read_fact(self) -> None:
        current = self.begin()
        good: dict[str, object] = {"status": "read", "checked": NOW.isoformat(), "releases": {"1.0.0": "^0.19"},
                "newest": "1.0.0", "ready": "", "repository": "owner/adapter"}
        cases: list[tuple[str, object]] = [("error", 1), ("crates_io", None),
                                         ("crates_io", {"status": "unknown"})]
        wrong_fields: tuple[tuple[str, object], ...] = (
            ("checked", 1), ("checked", "bad-time"), ("newest", 1),
            ("ready", None), ("repository", []), ("releases", []), ("releases", {"1.0.0": 1}),
        )
        for key, wrong in wrong_fields:
            invalid = dict(good)
            invalid[key] = wrong
            cases.append(("crates_io", invalid))
        incomplete = dict(good)
        del incomplete["ready"]
        cases.append(("crates_io", incomplete))
        for field, value in cases:
            with self.subTest(field=field, value=value), upgrade_record.locked("demo"):
                raw = cast(dict[str, object], json.loads(json.dumps(current)))
                dependencies = cast(dict[str, dict[str, object]], raw["dependencies"])
                dependencies["alpha"][field] = value
                _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(upgrade_record.RecordFailure):
                    _ = upgrade_record.load("demo")

    def test_apply_pins_retains_facts_errors_and_record_check_times(self) -> None:
        current = self.begin()
        facts: upgrade_record.CratesIoRead = {
            "status": "read", "checked": NOW.isoformat(), "releases": {"1.0.0": "^0.19"},
            "newest": "1.0.0", "ready": "", "repository": "owner/adapter",
        }
        current["dependencies"]["alpha"]["crates_io"] = facts
        current["dependencies"]["alpha"]["error"] = "crates.io: offline"
        current["target_bevy"] = {"status": "pre-release", "since": "2026-10-01"}
        current["target_error"] = "crates.io: HTTP 503"
        current["checked"] = NOW.isoformat()
        merged = upgrade_record.apply_pins(current, pins.Pins(("0.19.1",), frozenset({"bevy_ecs"}),
                                                             (dependent("alpha", "2.0.0"), dependent("beta"))), LATER)
        self.assertEqual(merged["dependencies"]["alpha"]["crates_io"], facts)
        self.assertEqual(merged["dependencies"]["alpha"]["error"], "crates.io: offline")
        self.assertEqual(merged["dependencies"]["beta"]["crates_io"], {"status": "unread"})
        self.assertEqual(merged["dependencies"]["beta"]["error"], "")
        self.assertEqual(merged["target_bevy"], {"status": "pre-release", "since": "2026-10-01"})
        self.assertEqual(merged["target_error"], "crates.io: HTTP 503")
        self.assertEqual(merged["checked"], NOW.isoformat())

    def test_load_adds_unread_branch_and_empty_items_to_previous_phase_record(self) -> None:
        current = self.begin()
        raw = cast(dict[str, object], json.loads(json.dumps(current)))
        for value in cast(dict[str, dict[str, object]], raw["dependencies"]).values():
            del value["branch"]
            del value["items"]
        with upgrade_record.locked("demo"):
            _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(upgrade_record.load("demo"), current)

    def test_unlisted_crate_branch_and_items_round_trip_and_survive_pin_refresh(self) -> None:
        current = self.begin()
        value = current["dependencies"]["alpha"]
        value["crates_io"] = {"status": "unlisted", "checked": NOW.isoformat(), "repository": "owner/adapter"}
        value["branch"] = {"status": "read", "repository": "owner/adapter", "name": "main", "rev": "abcdef0123456789",
                           "committed": "2026-10-12", "requirement": "^0.20"}
        value["items"] = [
            {"number": 7, "kind": "pr", "state": "merged", "title": "Port 0.20",
             "updated": NOW.isoformat(), "head": "port @ abcdef0"},
            {"number": 8, "kind": "issue", "state": "closed", "title": "Track 0.20",
             "updated": NOW.isoformat(), "head": ""},
        ]
        with upgrade_record.locked("demo"):
            upgrade_record.save(current)
            self.assertEqual(upgrade_record.load("demo"), current)
        branch, items, facts = value["branch"], value["items"], value["crates_io"]
        merged = upgrade_record.apply_pins(current, self.current_pins, LATER)
        self.assertEqual(merged["dependencies"]["alpha"]["branch"], branch)
        self.assertEqual(merged["dependencies"]["alpha"]["items"], items)
        self.assertEqual(merged["dependencies"]["alpha"]["crates_io"], facts)

    def test_added_github_fields_reject_wrong_types_missing_members_and_invalid_tags(self) -> None:
        current = self.begin()
        branch: dict[str, object] = {"status": "read", "repository": "owner/adapter", "name": "main", "rev": "abcdef",
                                    "committed": "2026-10-12", "requirement": "^0.20"}
        item: dict[str, object] = {"number": 7, "kind": "pr", "state": "open", "title": "Port",
                                  "updated": NOW.isoformat(), "head": "port @ abcdef0"}
        unlisted: dict[str, object] = {"status": "unlisted", "checked": NOW.isoformat(), "repository": ""}
        cases: list[tuple[str, object]] = [
            ("branch", None), ("branch", {"status": "unknown"}), ("items", None), ("items", {}),
            ("items", [1]), ("crates_io", {"status": "unlisted"}),
        ]
        for key in ("repository", "name", "rev", "committed", "requirement"):
            for wrong in (None, 1):
                invalid = dict(branch)
                invalid[key] = wrong
                cases.append(("branch", invalid))
            incomplete = dict(branch)
            del incomplete[key]
            cases.append(("branch", incomplete))
        wrong_items: tuple[tuple[str, object], ...] = (
            ("number", "7"), ("number", True), ("kind", "discussion"),
            ("state", "draft"), ("title", 1), ("updated", None), ("head", []),
        )
        for key, wrong in wrong_items:
            invalid = dict(item)
            invalid[key] = wrong
            cases.append(("items", [invalid]))
        for key in item:
            invalid = dict(item)
            del invalid[key]
            cases.append(("items", [invalid]))
        wrong_unlisted: tuple[tuple[str, object], ...] = (
            ("checked", 1), ("checked", "bad-time"), ("repository", None),
        )
        for key, wrong in wrong_unlisted:
            invalid = dict(unlisted)
            invalid[key] = wrong
            cases.append(("crates_io", invalid))
        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid), upgrade_record.locked("demo"):
                raw = cast(dict[str, object], json.loads(json.dumps(current)))
                values = cast(dict[str, dict[str, object]], raw["dependencies"])
                values["alpha"][field] = invalid
                _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(upgrade_record.RecordFailure):
                    _ = upgrade_record.load("demo")

    def test_read_branch_without_repository_is_refused(self) -> None:
        current = self.begin()
        raw = cast(dict[str, object], json.loads(json.dumps(current)))
        values = cast(dict[str, dict[str, object]], raw["dependencies"])
        values["alpha"]["branch"] = {"status": "read", "name": "main", "rev": "abcdef",
                                     "committed": "2026-10-12", "requirement": "^0.20"}
        with upgrade_record.locked("demo"):
            _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(upgrade_record.RecordFailure):
                _ = upgrade_record.load("demo")


    def test_load_adds_unannounced_baseline_to_previous_phase_record(self) -> None:
        current = self.begin()
        raw = cast(dict[str, object], json.loads(json.dumps(current)))
        del raw["announced"]
        with upgrade_record.locked("demo"):
            _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(upgrade_record.load("demo"), current)

    def test_announced_round_trips_and_pin_refresh_keeps_what_was_told(self) -> None:
        current = self.begin()
        current["announced"] = {"alpha": {"state": "port in PR", "repository": "owner/adapter",
                                            "items": {"7": "open", "8": "closed"}}}
        with upgrade_record.locked("demo"):
            upgrade_record.save(current)
            self.assertEqual(upgrade_record.load("demo"), current)
        merged = upgrade_record.apply_pins(current, self.current_pins, LATER)
        self.assertEqual(merged["announced"], current["announced"])

    def test_announced_present_fields_are_validated(self) -> None:
        current = self.begin()
        cases: tuple[object, ...] = (
            [], 1, {"alpha": None}, {"alpha": {"state": "done", "items": {}}},
            {"alpha": {"state": 1, "repository": "", "items": {}}},
            {"alpha": {"state": "done", "repository": None, "items": {}}},
            {"alpha": {"state": "done", "repository": "", "items": []}},
            {"alpha": {"state": "done", "repository": "", "items": {"7": 1}}},
        )
        for invalid in cases:
            with self.subTest(value=invalid), upgrade_record.locked("demo"):
                raw = cast(dict[str, object], json.loads(json.dumps(current)))
                raw["announced"] = invalid
                _ = upgrade_record.record_path("demo").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(upgrade_record.RecordFailure):
                    _ = upgrade_record.load("demo")


if __name__ == "__main__":
    _ = unittest.main()
