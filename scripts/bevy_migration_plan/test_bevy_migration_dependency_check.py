"""Exercise migration dependency discovery without cargo, network or a real checkout."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, final
from unittest import mock
from urllib import request
from urllib.error import URLError

from scripts.bevy_migration_plan import bevy_migration_dependency_check as checker

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method


TARGET = "0.20.0"
NAMES = frozenset({"bevy", "bevy_internal", "bevy_app", "bevy_ecs"})


def dependency(name: str, requirement: str, kind: str = "normal") -> dict[str, str]:
    return {"crate_id": name, "req": requirement, "kind": kind}


@final
class MigrationDependencyTests(unittest.TestCase):
    root: Path = Path()
    codebase: Path = Path()
    bodies: dict[str, object] = {}
    urls: list[str] = []

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="migration-dependencies-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.codebase = self.root / "project"
        self.codebase.mkdir()
        self.bodies = {}
        self.urls = []
        home_patch = mock.patch.object(Path, "home", return_value=self.root)
        network_patch = mock.patch.object(request, "urlopen", side_effect=self.urlopen)
        cargo_patch = mock.patch.object(subprocess, "run", side_effect=AssertionError("unexpected cargo command"))
        for patch in (home_patch, network_patch, cargo_patch):
            _ = patch.start()
            self.addCleanup(patch.stop)

    def urlopen(self, value: request.Request, timeout: int = 10) -> io.BytesIO:
        self.assertEqual(timeout, 10)
        url = value.full_url
        self.urls.append(url)
        if url not in self.bodies:
            raise AssertionError(f"unexpected URL: {url}")
        body = self.bodies[url]
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    def release(self, crate: str, version: str, dependencies: list[dict[str, str]]) -> None:
        self.bodies[f"https://crates.io/api/v1/crates/{crate}/{version}/dependencies"] = {
            "dependencies": dependencies}

    def failed_release(self, crate: str, version: str, reason: str = "offline") -> None:
        self.bodies[f"https://crates.io/api/v1/crates/{crate}/{version}/dependencies"] = URLError(reason)

    def crate(self, name: str, versions: list[str]) -> None:
        self.bodies[f"https://crates.io/api/v1/crates/{name}"] = {
            "crate": {"max_version": versions[0], "updated_at": "2026-10-12"},
            "versions": [{"num": version, "yanked": False} for version in versions]}

    def checkout(self) -> checker.BevyCheckoutRead:
        crates = self.root / f"rust/bevy-{TARGET}/crates"
        for name in ("bevy_app", "bevy_ecs"):
            directory = crates / name
            directory.mkdir(parents=True, exist_ok=True)
            _ = (directory / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n', encoding="utf-8")
        return checker.BevyCheckoutRead(NAMES)

    def run_main(self, tree: str) -> tuple[str, str, str]:
        output = self.root / "report.md"
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["checker", "--bevy-version", TARGET,
                                             "--codebase", str(self.codebase), "--output", str(output)]), \
                mock.patch.object(checker, "run_cargo_tree", return_value=tree), \
                mock.patch.object(checker, "get_workspace_members", return_value={"project"}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                checker.main()
            except SystemExit as error:
                self.assertEqual(error.code, 0)
        return output.read_text(encoding="utf-8"), stdout.getvalue(), stderr.getvalue()

    def test_bevy_requirement_takes_precedence_over_subcrates(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy_app", "^0.19"), dependency("bevy", "^0.20")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.BevyRequirement("^0.20"))

    def test_subcrate_requirement_is_read_when_bevy_is_absent(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy_app", "^0.20")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.BevyRequirement("^0.20"))

    def test_first_subcrate_is_selected_by_sorted_name(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy_ecs", "^0.19"), dependency("bevy_app", "^0.20")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.BevyRequirement("^0.20"))

    def test_third_party_bevy_name_is_not_a_bevy_dependency(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy_egui", "^0.20")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.NoBevyDependency())

    def test_dev_bevy_dependency_is_not_a_runtime_requirement(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy", "^0.20", "dev")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.NoBevyDependency())

    def test_dev_bevy_does_not_override_normal_subcrate(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy", "^0.19", "dev"), dependency("bevy_ecs", "^0.20")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.BevyRequirement("^0.20"))

    def test_build_subcrate_dependency_is_a_bevy_requirement(self) -> None:
        self.release("adapter", "1.0.0", [dependency("bevy_app", "^0.20", "build")])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.BevyRequirement("^0.20"))

    def test_failed_request_returns_unknown_requirement_with_reason(self) -> None:
        self.failed_release("adapter", "1.0.0")
        result = checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES)
        self.assertIsInstance(result, checker.RequirementLookupFailed)
        assert isinstance(result, checker.RequirementLookupFailed)
        self.assertIn("offline", result.reason)

    def test_missing_dependency_kind_returns_an_explicit_response_failure(self) -> None:
        self.release("adapter", "1.0.0", [{"crate_id": "bevy", "req": "^0.20"}])
        self.assertEqual(checker.get_bevy_dependency_requirement("adapter", "1.0.0", NAMES),
                         checker.RequirementLookupFailed("crates.io response is missing 'kind'"))

    def test_discovery_keeps_subcrate_only_and_failed_lookup_crates(self) -> None:
        checkout = self.checkout()
        self.release("scene-save", "1.0.0", [dependency("bevy_ecs", "^0.20")])
        self.failed_release("unknown-adapter", "1.0.0")
        self.release("bevy-third-party", "1.0.0", [dependency("bevy_egui", "^0.20")])
        tree = "project v0.1.0\n├── scene-save v1.0.0\n├── unknown-adapter v1.0.0\n└── bevy-third-party v1.0.0"
        with mock.patch.object(checker, "run_cargo_tree", return_value=tree), \
                mock.patch.object(checker, "get_workspace_members", return_value={"project"}):
            discovered = checker.get_bevy_dependencies(self.codebase, checkout)
        self.assertEqual([(item.name, item.version) for item in discovered],
                         [("scene-save", "1.0.0"), ("unknown-adapter", "1.0.0")])
        self.assertEqual(discovered[0], checker.DiscoveredDependency(
            "scene-save", "1.0.0", checker.BevyRequirement("^0.20")))
        lookup = discovered[1].lookup
        self.assertIsInstance(lookup, checker.RequirementLookupFailed)
        assert isinstance(lookup, checker.RequirementLookupFailed)
        self.assertIn("offline", lookup.reason)

    def test_compatible_version_is_newest_matching_subcrate_release(self) -> None:
        self.release("scene-save", "3.0.0", [dependency("bevy_ecs", "^0.19")])
        for version in ("2.0.0", "1.0.0"):
            self.release("scene-save", version, [dependency("bevy_ecs", "^0.20")])
        result = checker.find_bevy_compatible_version("scene-save", ["3.0.0", "2.0.0", "1.0.0"],
                                                     TARGET, self.checkout())
        self.assertEqual(result, checker.CompatibleVersion("2.0.0"))
        self.assertEqual(len(self.urls), 2)

    def test_matching_release_wins_even_when_another_lookup_failed(self) -> None:
        self.failed_release("adapter", "2.0.0")
        self.release("adapter", "1.0.0", [dependency("bevy", "^0.20")])
        self.assertEqual(checker.find_bevy_compatible_version("adapter", ["2.0.0", "1.0.0"], TARGET,
                                                             self.checkout()), checker.CompatibleVersion("1.0.0"))

    def test_no_match_with_failed_lookup_returns_first_failure(self) -> None:
        self.failed_release("adapter", "3.0.0", "first failure")
        self.release("adapter", "2.0.0", [dependency("bevy_app", "^0.19")])
        self.failed_release("adapter", "1.0.0", "later failure")
        result = checker.find_bevy_compatible_version("adapter", ["3.0.0", "2.0.0", "1.0.0"],
                                                     TARGET, self.checkout())
        self.assertIsInstance(result, checker.CompatibilityUnknown)
        assert isinstance(result, checker.CompatibilityUnknown)
        self.assertIn("first failure", result.reason)
        self.assertNotIn("later failure", result.reason)

    def test_no_matching_requirement_is_no_compatible_version(self) -> None:
        self.release("adapter", "2.0.0", [dependency("bevy_app", "^0.19")])
        self.release("adapter", "1.0.0", [])
        self.assertEqual(checker.find_bevy_compatible_version("adapter", ["2.0.0", "1.0.0"], TARGET,
                                                             self.checkout()), checker.NoCompatibleVersion())

    def test_compatibility_reads_only_twenty_recent_versions(self) -> None:
        versions = [f"{number}.0.0" for number in range(21, 0, -1)]
        for version in versions[:20]:
            self.release("adapter", version, [dependency("bevy_app", "^0.19")])
        self.release("adapter", versions[20], [dependency("bevy_app", "^0.20")])
        self.assertEqual(checker.find_bevy_compatible_version("adapter", versions, TARGET, self.checkout()),
                         checker.NoCompatibleVersion())
        self.assertEqual(len(self.urls), 20)

    def test_checkout_reads_own_crate_names_and_always_includes_bevy(self) -> None:
        expected = self.checkout()
        self.assertEqual(checker.get_bevy_internal_crates(TARGET), expected)

    def test_missing_checkout_is_explicit_and_keeps_direct_bevy_crates(self) -> None:
        missing = checker.get_bevy_internal_crates(TARGET)
        self.assertEqual(missing, checker.BevyCheckoutMissing(self.root / f"rust/bevy-{TARGET}/crates"))
        with mock.patch.object(checker, "run_cargo_tree", return_value="project v0.1.0\n└── bevy_app v0.20.0"), \
                mock.patch.object(checker, "get_workspace_members", return_value={"project"}):
            self.assertEqual(checker.get_all_direct_dependencies(self.codebase, missing), [("bevy_app", "0.20.0")])

    def test_read_checkout_excludes_only_own_crates_from_direct_dependencies(self) -> None:
        checkout = self.checkout()
        tree = "project v0.1.0\n├── bevy_app v0.20.0\n├── bevy_internal v0.20.0\n└── bevy_egui v1.0.0"
        with mock.patch.object(checker, "run_cargo_tree", return_value=tree), \
                mock.patch.object(checker, "get_workspace_members", return_value={"project"}):
            self.assertEqual(checker.get_all_direct_dependencies(self.codebase, checkout), [("bevy_egui", "1.0.0")])

    def test_missing_checkout_compatibility_reads_bevy_only(self) -> None:
        missing = checker.get_bevy_internal_crates(TARGET)
        self.release("adapter", "2.0.0", [dependency("bevy_ecs", "^0.20")])
        self.release("adapter", "1.0.0", [dependency("bevy", "^0.20")])
        self.assertEqual(checker.find_bevy_compatible_version("adapter", ["2.0.0", "1.0.0"], TARGET, missing),
                         checker.CompatibleVersion("1.0.0"))

    def test_main_reads_checkout_once_and_writes_unknown_compatibility_for_failed_release(self) -> None:
        _ = self.checkout()
        self.failed_release("scene-save", "1.0.0", "release unavailable")
        self.crate("scene-save", ["2.0.0", "1.0.0"])
        self.release("scene-save", "2.0.0", [dependency("bevy_ecs", "^0.19")])
        with mock.patch.object(checker, "get_bevy_internal_crates", wraps=checker.get_bevy_internal_crates) as read:
            report, stdout, _ = self.run_main("project v0.1.0\n└── scene-save v1.0.0")
            read.assert_called_once_with(TARGET)
        self.assertEqual(stdout, "")
        self.assertIn("### ⚠️ Verification Needed", report)
        self.assertIn('**`scene-save = "1.0.0"`**', report)
        self.assertIn("Could not read every release's dependencies from crates.io (", report)
        self.assertIn("release unavailable", report)
        self.assertIn(") - manual verification needed", report)
        self.assertNotIn("### 🚫 Blockers", report)

    def test_main_failed_discovery_without_a_matching_release_needs_verification(self) -> None:
        _ = self.checkout()
        self.failed_release("scene-save", "1.0.0", "pinned release unavailable")
        self.crate("scene-save", ["3.0.0", "2.0.0"])
        for version in ("3.0.0", "2.0.0"):
            self.release("scene-save", version, [])
        report, _, _ = self.run_main("project v0.1.0\n└── scene-save v1.0.0")
        self.assertIn("### ⚠️ Verification Needed", report)
        self.assertIn('**`scene-save = "1.0.0"`**', report)
        reason = ("Could not read release 1.0.0's dependencies from crates.io "
                  f"({URLError('pinned release unavailable')}) - manual verification needed")
        self.assertIn(reason, report)
        self.assertNotIn("### 🚫 Blockers", report)
        self.assertEqual(self.urls.count(
            "https://crates.io/api/v1/crates/scene-save/1.0.0/dependencies"), 1)

    def test_main_failed_discovery_with_a_matching_release_is_an_update(self) -> None:
        _ = self.checkout()
        self.failed_release("scene-save", "1.0.0", "pinned release unavailable")
        self.crate("scene-save", ["2.0.0"])
        self.release("scene-save", "2.0.0", [dependency("bevy_ecs", "^0.20")])
        report, _, _ = self.run_main("project v0.1.0\n└── scene-save v1.0.0")
        self.assertIn("### 🔄 Updates Required", report)
        self.assertIn('**`scene-save = "1.0.0"`** → `"2.0.0"`', report)
        self.assertIn("Update to 2.0.0 for Bevy 0.20.0 support", report)
        self.assertNotIn("### ⚠️ Verification Needed", report)
        self.assertNotIn("### 🚫 Blockers", report)

    def test_main_known_bevy_dependency_without_a_matching_release_is_a_blocker(self) -> None:
        _ = self.checkout()
        self.release("scene-save", "1.0.0", [dependency("bevy_ecs", "^0.19")])
        self.crate("scene-save", ["2.0.0", "1.0.0"])
        self.release("scene-save", "2.0.0", [dependency("bevy_ecs", "^0.19")])
        report, _, _ = self.run_main("project v0.1.0\n└── scene-save v1.0.0")
        self.assertIn("### 🚫 Blockers", report)
        self.assertIn('**`scene-save = "1.0.0"`**', report)
        self.assertIn("No version found compatible with Bevy 0.20.0. Latest version: 2.0.0", report)
        self.assertNotIn("### ⚠️ Verification Needed", report)

    def test_main_missing_checkout_warns_once_in_stderr_and_report(self) -> None:
        self.release("bevy-adapter", "1.0.0", [dependency("bevy", "^0.20")])
        self.crate("bevy-adapter", ["1.0.0"])
        report, _, stderr = self.run_main("project v0.1.0\n└── bevy-adapter v1.0.0")
        warning = (f"Warning: {self.root / f'rust/bevy-{TARGET}/crates'} not found; "
                   "crates that depend only on a Bevy sub-crate are not detected")
        self.assertEqual(stderr.count(warning), 1)
        self.assertEqual(report.count(warning), 1)
        self.assertLess(report.index(warning), report.index("**Status:**"))
        self.assertIn("### ✅ Compatible Dependencies", report)

    def test_main_missing_checkout_marks_even_empty_report_incomplete(self) -> None:
        report, _, stderr = self.run_main("project v0.1.0")
        self.assertIn("crates that depend only on a Bevy sub-crate are not detected", stderr)
        self.assertIn("crates that depend only on a Bevy sub-crate are not detected", report)
        self.assertIn("No bevy-related dependencies found", report)


if __name__ == "__main__":
    _ = unittest.main()
