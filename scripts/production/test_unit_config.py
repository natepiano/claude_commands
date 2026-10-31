"""Unit configuration survives retirement, interrupted publication and resumed launch."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tomllib
from typing import cast
from unittest.mock import patch

from scripts.lib import atomic_file
from scripts.lib.git_exclude import ExcludeRefused
from scripts.production import unit_config as config
from scripts.production.unit_config_test_support import UnitConfigFixture


class UnitConfigTests(UnitConfigFixture):
    def test_preview_read_keeps_abandoned_files_until_a_recovery_read(self) -> None:
        config.write(self.worktree, self.config())
        dead = self.source.with_name(f".{self.source.name}.999999999.abandoned.tmp")
        _ = dead.write_text('plan="unfinished')
        before = self.file_versions(), dead.read_bytes(), dead.stat().st_mtime_ns
        self.assertEqual(config.read(self.worktree, cleanup=False), self.config())
        self.assertTrue(dead.exists())
        self.assertEqual((self.file_versions(), dead.read_bytes(), dead.stat().st_mtime_ns), before)
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertFalse(dead.exists())

    def test_plan_states_port_and_responsibilities_round_trip_without_tracking(self) -> None:
        for plan in (config.PlanFile(Path("docs/a plan.md")), config.StandbyPlan(), config.PermanentPlan()):
            for port in (config.NoAppPort(), config.AppPort(8124)):
                with self.subTest(plan=plan, port=port):
                    value = config.UnitConfig(plan, port, ("nightly-review",))
                    config.write(self.worktree, value)
                    self.assertEqual(config.read(self.worktree), value)
                    fields = tomllib.loads(self.source.read_text())
                    self.assertEqual(fields["responsibilities"], ["nightly-review"])
                    self.assertEqual("port" in fields, isinstance(port, config.AppPort))
                    self.assertNotIn("retired", fields)
                    before = self.file_versions()
                    config.write(self.worktree, value)
                    self.assertEqual(self.file_versions(), before)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git("ls-files", ".unit.toml"), "")
        self.assertEqual(self.git("check-ignore", ".unit.toml"), ".unit.toml")

    def test_missing_config_is_distinct_from_malformed_or_unreadable_config(self) -> None:
        missing = config.read(self.worktree)
        self.assertIsInstance(missing, config.ConfigMissing)
        if isinstance(missing, config.ConfigMissing):
            self.assertIn(str(self.source), missing.reason)
        for text in ('plan = [', 'plan="standby"\nport="1234"\nresponsibilities=[]\n',
                     'plan="../elsewhere"\nresponsibilities=[]\n',
                     'plan="/absolute"\nresponsibilities=[]\n',
                     'plan="standby"\nresponsibilities="nightly-review"\n'):
            with self.subTest(text=text):
                _ = self.source.write_text(text)
                with self.assertRaises(config.ConfigRefused) as caught:
                    _ = config.read(self.worktree)
                self.assertIn(str(self.source), str(caught.exception))
                self.assertEqual(self.source.read_text(), text)
        _ = self.source.write_bytes(b'plan="\xff"')
        with self.assertRaises(config.ConfigRefused):
            _ = config.read(self.worktree)

    def test_park_restore_and_discard_keep_retirement_facts_until_the_last_step(self) -> None:
        self.assertIsInstance(config.place(self.unit, self.worktree), config.Nowhere)
        config.write(self.worktree, self.config())
        source = self.source.read_bytes()
        self.assertEqual(config.place(self.unit, self.worktree), config.InWorktree(self.source))
        config.park(self.unit, self.worktree, self.retirement())
        self.assertEqual(self.source.read_bytes(), source)
        facts = cast(dict[str, object], tomllib.loads(self.parked.read_text())["retired"])
        self.assertEqual(facts, {"date": "2026-10-31", "session": "session-before-retire",
                                 "folder": str(self.worktree), "branch": "feature/worker"})
        self.assertEqual(config.place(self.unit, self.worktree), config.Both(self.source, self.parked, True))
        self.source.unlink()
        self.assertEqual(config.place(self.unit, self.worktree), config.Parked(self.parked))
        parked = self.parked.read_bytes(), self.parked.stat().st_mtime_ns
        config.restore(self.unit, self.worktree)
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertEqual((self.parked.read_bytes(), self.parked.stat().st_mtime_ns), parked)
        self.assertNotIn("retired", tomllib.loads(self.source.read_text()))
        config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        before = self.file_versions()
        config.discard_parked(self.unit)
        config.restore(self.unit, self.worktree)
        self.assertEqual(self.file_versions(), before)

    def test_unrecorded_start_folder_is_retained_without_inventing_a_path(self) -> None:
        config.write(self.worktree, self.config())
        retired = replace(self.retirement(), start_folder=config.UnrecordedStartFolder())
        config.park(self.unit, self.worktree, retired)
        self.assertEqual(tomllib.loads(self.parked.read_text())["retired"]["folder"], "")
        before = self.file_versions()
        config.park(self.unit, self.worktree, retired)
        self.assertEqual(self.file_versions(), before)

    def test_park_after_worktree_removal_uses_verified_retirement_and_changes_nothing(self) -> None:
        self.park_config()
        self.source.unlink()
        before = self.file_versions()
        config.park(self.unit, self.worktree, self.retirement())
        self.assertEqual(self.file_versions(), before)
        with self.assertRaises(config.ConfigRefused):
            config.park(self.unit, self.worktree, replace(self.retirement(), session_id="another-session"))
        self.assertEqual(self.file_versions(), before)

    def test_missing_copies_refuse_park_and_restore_without_creating_evidence(self) -> None:
        for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                          lambda: config.restore(self.unit, self.worktree)):
            with self.assertRaises(config.ConfigRefused):
                operation()
            self.assertFalse(self.source.exists())
            self.assertFalse(self.parked.exists())

    def test_malformed_parked_evidence_is_never_overwritten_or_used_for_restore(self) -> None:
        self.park_config()
        _ = self.parked.write_text('[retired]\ndate="unfinished"\n')
        before = self.file_versions()
        for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                          lambda: config.restore(self.unit, self.worktree)):
            with self.assertRaises(config.ConfigRefused) as caught:
                operation()
            self.assertIn(str(self.parked), str(caught.exception))
            self.assertEqual(self.file_versions(), before)

    def test_publication_is_read_back_before_park_or_restore_reports_success(self) -> None:
        config.write(self.worktree, self.config())
        real_replace = os.replace
        for destination in (self.parked, self.source):
            with self.subTest(destination=destination):
                if destination == self.source:
                    self.parked.unlink(missing_ok=True)
                    config.park(self.unit, self.worktree, self.retirement())
                    self.source.unlink()

                def corrupt_publication(temporary: Path, path: Path) -> None:
                    if path == destination:
                        _ = temporary.write_text(temporary.read_text().replace("8123", "8126"))
                    real_replace(temporary, path)

                with patch.object(os, "replace", side_effect=corrupt_publication):
                    with self.assertRaises(config.ConfigRefused) as caught:
                        if destination == self.parked:
                            config.park(self.unit, self.worktree, self.retirement())
                        else:
                            config.restore(self.unit, self.worktree)
                self.assertIn(str(destination), str(caught.exception))
                retained = self.source if destination == self.parked else self.parked
                self.assertEqual(tomllib.loads(retained.read_text())["port"], 8123)

    def test_different_copies_refuse_both_operations_and_name_paths_and_first_key(self) -> None:
        self.park_config()
        for key, different in (
            ("plan", replace(self.config(), plan=config.StandbyPlan(), port=config.NoAppPort())),
            ("port", replace(self.config(), port=config.NoAppPort(), responsibilities=())),
            ("responsibilities", replace(self.config(), responsibilities=())),
        ):
            config.write(self.worktree, different)
            self.assertEqual(config.place(self.unit, self.worktree), config.Both(self.source, self.parked, False))
            before = self.file_versions()
            for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                              lambda: config.restore(self.unit, self.worktree)):
                with self.subTest(key=key), self.assertRaises(config.ConfigRefused) as caught:
                    operation()
                message = str(caught.exception)
                self.assertIn(str(self.source), message)
                self.assertIn(str(self.parked), message)
                self.assertIn(key, message)
                self.assertEqual(len(message.splitlines()), 1)
                self.assertEqual(self.file_versions(), before)

    def test_different_retirement_facts_refuse_without_replacing_the_saved_session(self) -> None:
        self.park_config()
        before = self.file_versions()
        with self.assertRaisesRegex(config.ConfigRefused, "retired"):
            config.park(self.unit, self.worktree, replace(self.retirement(), branch=config.Branch("other")))
        self.assertEqual(self.file_versions(), before)

    def test_matching_copies_make_park_and_restore_reruns_noops(self) -> None:
        self.park_config()
        before = self.file_versions()
        config.park(self.unit, self.worktree, self.retirement())
        config.restore(self.unit, self.worktree)
        self.assertEqual(self.file_versions(), before)

    def test_park_interrupted_before_or_after_publication_recovers_without_removing_the_source(self) -> None:
        config.write(self.worktree, self.config())
        source = self.source.read_bytes()
        real_replace = os.replace
        for published in (False, True):
            with self.subTest(published=published):
                self.parked.unlink(missing_ok=True)

                def interrupt(temporary: Path, destination: Path) -> None:
                    if destination == self.parked:
                        if published:
                            real_replace(temporary, destination)
                        raise OSError("stopped at parked publication")
                    real_replace(temporary, destination)

                with patch.object(os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "stopped at parked publication"):
                        config.park(self.unit, self.worktree, self.retirement())
                self.assertEqual(self.source.read_bytes(), source)
                self.assertEqual(self.parked.exists(), published)
                if published:
                    self.assertEqual(tomllib.loads(self.parked.read_text())["retired"]["session"],
                                     "session-before-retire")
                config.park(self.unit, self.worktree, self.retirement())
                complete = self.file_versions()
                config.park(self.unit, self.worktree, self.retirement())
                self.assertEqual(self.file_versions(), complete)
                self.assertEqual(list(self.parked.parent.glob(f".{self.parked.name}.*")), [])

    def test_restore_interrupted_before_or_after_publication_keeps_the_parked_facts_and_recovers(self) -> None:
        self.park_config()
        saved = self.parked.read_bytes(), self.parked.stat().st_mtime_ns
        real_replace = os.replace
        for published in (False, True):
            with self.subTest(published=published):
                self.source.unlink(missing_ok=True)

                def interrupt(temporary: Path, destination: Path) -> None:
                    if destination == self.source:
                        if published:
                            real_replace(temporary, destination)
                        raise OSError("stopped at worktree publication")
                    real_replace(temporary, destination)

                with patch.object(os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "stopped at worktree publication"):
                        config.restore(self.unit, self.worktree)
                self.assertEqual((self.parked.read_bytes(), self.parked.stat().st_mtime_ns), saved)
                self.assertEqual(self.source.exists(), published)
                if published:
                    self.assertEqual(config.read(self.worktree), self.config())
                config.restore(self.unit, self.worktree)
                complete = self.file_versions()
                config.restore(self.unit, self.worktree)
                self.assertEqual(self.file_versions(), complete)

    def test_discard_interrupted_before_unlink_preserves_facts_and_retry_removes_only_parked_file(self) -> None:
        self.park_config()
        before = self.file_versions()
        real_unlink = Path.unlink

        def interrupt(path: Path, missing_ok: bool = False) -> None:
            if path == self.parked:
                raise PermissionError("stopped before parked unlink")
            real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", interrupt):
            with self.assertRaisesRegex(OSError, "stopped before parked unlink"):
                config.discard_parked(self.unit)
        self.assertEqual(self.file_versions(), before)
        config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        self.assertEqual(self.source.read_bytes(), before[self.source][0])
        complete = self.file_versions()
        config.discard_parked(self.unit)
        self.assertEqual(self.file_versions(), complete)

    def test_discard_interrupted_after_unlink_leaves_restored_config_and_safe_rerun(self) -> None:
        self.park_config()
        source = self.source.read_bytes(), self.source.stat().st_mtime_ns
        with patch.object(atomic_file, "sync_folder", side_effect=OSError("stopped after parked unlink")):
            with self.assertRaisesRegex(OSError, "stopped after parked unlink"):
                config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        config.discard_parked(self.unit)
        config.restore(self.unit, self.worktree)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), source)

    def test_interrupted_replacement_keeps_the_old_whole_config_until_retry(self) -> None:
        config.write(self.worktree, self.config())
        before = self.file_versions()
        updated = replace(self.config(), port=config.AppPort(8125))
        with patch.object(os, "replace", side_effect=OSError("stopped before replace")):
            with self.assertRaisesRegex(OSError, "stopped before replace"):
                config.write(self.worktree, updated)
        self.assertEqual(self.file_versions(), before)
        config.write(self.worktree, updated)
        self.assertEqual(config.read(self.worktree), updated)
        self.assertEqual(list(self.worktree.glob("..unit.toml.*")), [])

    def test_read_removes_an_abandoned_temporary_file_and_keeps_the_last_complete_config(self) -> None:
        config.write(self.worktree, self.config())
        dead = self.source.with_name(f".{self.source.name}.999999999.abandoned.tmp")
        _ = dead.write_text('plan="unfinished')
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertFalse(dead.exists())

    def test_tracked_or_unignored_worktree_file_cannot_be_overwritten_by_write_or_restore(self) -> None:
        self.park_config()
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    _ = self.git("add", "-f", ".unit.toml")
                else:
                    _ = (self.worktree / ".gitignore").write_text("!/.unit.toml\n")
                before = self.file_versions()
                for operation in (lambda: config.write(self.worktree, self.config()),
                                  lambda: config.restore(self.unit, self.worktree)):
                    with self.assertRaises((ExcludeRefused, config.ConfigRefused)):
                        operation()
                    self.assertEqual(self.file_versions(), before)

    def test_restore_refuses_unsafe_git_rules_before_publishing_a_missing_worktree_copy(self) -> None:
        self.park_config()
        self.source.unlink()
        _ = (self.worktree / ".gitignore").write_text("!/.unit.toml\n")
        saved = self.parked.read_bytes()
        with self.assertRaises((ExcludeRefused, config.ConfigRefused)):
            config.restore(self.unit, self.worktree)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.parked.read_bytes(), saved)
