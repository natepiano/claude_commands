"""Pin the migration counter's public CLI against real Rust source trees."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method


CountMode = Literal["single", "multiple", "verify"]
MODES: tuple[CountMode, ...] = ("single", "multiple", "verify")
COUNTER = Path(__file__).resolve().with_name("bevy_migration_count_pattern.sh")


@final
class MigrationPatternCountTests(unittest.TestCase):
    root: Path = Path()
    codebase: Path = Path()

    @override
    def setUp(self) -> None:
        temporary_root = Path("/tmp/claude")
        temporary_root.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(prefix="migration-counter-", dir=temporary_root)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.codebase = self.root / "project"
        self.codebase.mkdir()

    def source(self, relative_path: str, contents: str) -> None:
        path = self.codebase / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(contents, encoding="utf-8")

    def count(
        self,
        mode: CountMode,
        patterns: tuple[str, ...],
        codebase: Path,
        pass1_total: int = 0,
        file_type: str = "rust",
    ) -> subprocess.CompletedProcess[str]:
        if mode == "single":
            self.assertEqual(len(patterns), 1)
            arguments = [patterns[0], str(codebase), file_type]
        elif mode == "multiple":
            arguments = ["--multiple", *patterns, "--", str(codebase), file_type]
        else:
            arguments = ["--verify", "--pass1-total", str(pass1_total), "--patterns",
                         *patterns, "--", str(codebase), file_type]
        return subprocess.run(
            ["bash", str(COUNTER), *arguments], cwd=self.root,
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def report(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return cast("dict[str, object]", json.loads(result.stdout))

    def assert_count(self, mode: CountMode, pattern: str, expected: int) -> None:
        result = self.count(mode, (pattern,), self.codebase, pass1_total=expected)
        if mode == "single":
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout.strip(), str(expected))
        elif mode == "multiple":
            self.assertEqual(self.report(result), {pattern: expected, "_total": expected})
        else:
            self.assertEqual(self.report(result), {
                "pass1_total": expected,
                "breakdown": {pattern: expected},
                "pass2_total": expected,
                "variance_percent": 0,
                "status": "MATCH",
            })

    def test_zero_matches_succeed_and_return_zero_in_every_mode(self) -> None:
        self.source("src/main.rs", "fn main() {}\n")
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assert_count(mode, "MissingType", 0)

    def test_json_modes_preserve_backslash_and_quote_pattern_keys(self) -> None:
        self.source("src/main.rs", 'let value = "Commands";\nCommands::new();\n')
        patterns = (r"\bCommands\b", '"Commands"')
        for mode in ("multiple", "verify"):
            with self.subTest(mode=mode):
                result = self.count(mode, patterns, self.codebase, pass1_total=3)
                expected: dict[str, object]
                if mode == "multiple":
                    expected = {patterns[0]: 2, patterns[1]: 1, "_total": 3}
                else:
                    expected = {
                        "pass1_total": 3,
                        "breakdown": {patterns[0]: 2, patterns[1]: 1},
                        "pass2_total": 3,
                        "variance_percent": 0,
                        "status": "MATCH",
                    }
                self.assertEqual(self.report(result), expected)

    def test_json_modes_continue_after_an_unmatched_pattern(self) -> None:
        self.source("src/main.rs", "Commands::new();\n")
        for mode in ("multiple", "verify"):
            with self.subTest(mode=mode):
                result = self.count(mode, ("MissingType", "Commands"), self.codebase, pass1_total=1)
                report = self.report(result)
                if mode == "multiple":
                    self.assertEqual(report, {"MissingType": 0, "Commands": 1, "_total": 1})
                else:
                    self.assertEqual(report["breakdown"], {"MissingType": 0, "Commands": 1})
                    self.assertEqual(report["pass2_total"], 1)
                    self.assertEqual(report["status"], "MATCH")

    def test_bad_regex_fails_with_diagnostic_in_every_mode(self) -> None:
        self.source("src/main.rs", "Commands::new();\n")
        for mode in MODES:
            with self.subTest(mode=mode):
                result = self.count(mode, ("[",), self.codebase)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("regex parse error", result.stderr)
                self.assertNotRegex(result.stdout, r'(?m)^\s*(?:"\[":\s*)?0\s*,?\s*$')

    def test_missing_path_fails_with_diagnostic_in_every_mode(self) -> None:
        missing_path = self.root / "missing-project"
        for mode in MODES:
            with self.subTest(mode=mode):
                result = self.count(mode, ("Commands",), missing_path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(missing_path), result.stderr)
                self.assertNotRegex(result.stdout, r'(?m)^\s*(?:"Commands":\s*)?0\s*,?\s*$')

    def test_multiple_occurrences_on_one_line_are_each_counted(self) -> None:
        self.source("src/main.rs", "Commands Commands Commands\nCommands\n")
        self.source("src/other.rs", "Commands Commands\n")
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assert_count(mode, "Commands", 6)

    def test_vendor_and_target_trees_are_excluded_in_every_mode(self) -> None:
        self.source("src/main.rs", "ExtractSchedule\n")
        for directory in ("vendor", "target", "crates/example/vendor", "crates/example/target"):
            self.source(f"{directory}/patched.rs", "ExtractSchedule ExtractSchedule\n")
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assert_count(mode, "ExtractSchedule", 1)

    def test_explicit_file_type_selects_only_requested_sources(self) -> None:
        self.source("src/main.rs", "Commands Commands Commands\n")
        self.source("helper.py", "Commands Commands\n")
        for mode in MODES:
            with self.subTest(mode=mode):
                result = self.count(mode, ("Commands",), self.codebase, pass1_total=2, file_type="py")
                if mode == "single":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "2")
                elif mode == "multiple":
                    self.assertEqual(self.report(result), {"Commands": 2, "_total": 2})
                else:
                    report = self.report(result)
                    self.assertEqual(report["breakdown"], {"Commands": 2})
                    self.assertEqual(report["pass2_total"], 2)


if __name__ == "__main__":
    _ = unittest.main()
