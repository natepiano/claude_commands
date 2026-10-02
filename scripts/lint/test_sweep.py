#!/usr/bin/env python3
"""Tests for sweep.py on a fabricated target directory: which unhashed files
go with a build unit, which are orphans, and which stay."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import sweep

GIB = 1 << 30
SIZE = 64 * 1024
DAY = 86_400
APP = "0123456789abcdef"
DEMO = "1111111111111111"
TOOL = "2222222222222222"
MY_TOOL = "3333333333333333"
STALE = "4444444444444444"


def write(path: Path, size: int = SIZE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Random bytes, so no filesystem stores the file sparse or deduplicated.
    _ = path.write_bytes(os.urandom(size))
    return path


def dep_info(copy: Path, output: str | None = None) -> Path:
    path = copy.with_suffix(".d")
    _ = path.write_text(f"{output or copy}: /src/main.rs\n")
    return path


def unit(tree: Path, directory: str, name: str, digest: str, used: float, size: int = SIZE) -> Path:
    """A build unit's fingerprint dir, last used at used, and its own file."""
    fingerprint = tree / ".fingerprint" / f"{name}-{digest}"
    fingerprint.mkdir(parents=True)
    record = fingerprint / f"bin-{name}.json"
    _ = record.write_text("{}")
    os.utime(record, (used, used))
    os.utime(fingerprint, (used, used))
    return write(tree / directory / f"{name}-{digest}", size)


class Target:
    """target/debug with live copies, orphaned copies and files nothing claims."""

    def __init__(self, base: Path) -> None:
        now = time.time()
        self.root: Path = base / "target"
        self.tree: Path = self.root / "debug"
        tree = self.tree
        examples = tree / "examples"
        # Live: hard-linked on Linux, as cargo does there.
        self.app: Path = tree / "app"
        os.link(unit(tree, "deps", "app", APP, now - 3 * DAY), self.app)
        self.app_dep_info: Path = dep_info(self.app)
        self.demo: Path = examples / "demo"
        os.link(unit(tree, "examples", "demo", DEMO, now), self.demo)
        self.demo_dep_info: Path = dep_info(self.demo)
        # Live: copied, as cargo does on macOS.
        self.tool: Path = tree / "tool"
        _ = shutil.copy2(unit(tree, "deps", "tool", TOOL, now), self.tool)
        self.tool_dep_info: Path = dep_info(self.tool)
        # Live: a hyphenated bin name, whose unit file carries the crate name.
        self.my_tool: Path = tree / "my-tool"
        os.link(unit(tree, "deps", "my_tool", MY_TOOL, now), self.my_tool)
        # Orphans: a copy with no unit of its name, a copy of a unit that was
        # rebuilt at another size, and a dep-info whose copy is gone.
        self.old: Path = write(examples / "old")
        stale_unit = unit(tree, "deps", "stale", STALE, now, SIZE // 2)
        self.stale: Path = write(tree / "stale")
        self.gone_dep_info: Path = dep_info(examples / "gone")
        self.orphans: set[Path] = {
            self.old,
            dep_info(self.old),
            self.stale,
            dep_info(self.stale),
            self.gone_dep_info,
        }
        self.stale_unit: Path = stale_unit
        # Nothing claims these: no dep-info, a dep-info naming another
        # directory, and a second link from outside any unit.
        linked = write(self.root / "tmp" / "linked")
        self.linked: Path = tree / "linked"
        os.link(linked, self.linked)
        self.unclaimed: set[Path] = {
            write(tree / "notes.txt"),
            write(examples / "stray"),
            write(examples / "elsewhere"),
            dep_info(examples / "elsewhere", "/other/place/elsewhere"),
            self.linked,
            dep_info(self.linked),
        }


def scanned(target: Target) -> tuple[sweep.Scan, dict[str, sweep.Group]]:
    scan = sweep.Scan()
    owners = sweep.group_roots(sweep.build_trees(str(target.root)), scan)
    sweep.walk(str(target.root), None, owners, scan)
    return scan, owners


def run_sweep(target: Target, budget: int, dry_run: bool) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output):
        status = sweep.sweep([str(target.root)], sweep.build_trees(str(target.root)), budget, "test", dry_run)
    return status, output.getvalue()


class SweepCase(unittest.TestCase):
    def target(self) -> Target:
        return Target(Path(self.enterContext(tempfile.TemporaryDirectory())).resolve())


class GroupTests(SweepCase):
    def test_hard_linked_copy_and_dep_info_join_their_unit(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        group = owners[str(target.tree / "deps" / f"app-{APP}")]
        self.assertIs(owners[str(target.app)], group)
        self.assertIs(owners[str(target.app_dep_info)], group)
        self.assertIs(owners[str(target.demo)], owners[str(target.tree / "examples" / f"demo-{DEMO}")])
        self.assertIs(owners[str(target.demo_dep_info)], owners[str(target.demo)])

    def test_copy_that_is_not_a_link_joins_the_unit_of_its_name_and_size(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        self.assertIs(owners[str(target.tool)], owners[str(target.tree / "deps" / f"tool-{TOOL}")])
        self.assertIs(owners[str(target.tool_dep_info)], owners[str(target.tool)])

    def test_hyphenated_copy_joins_its_crate_file(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        self.assertIs(owners[str(target.my_tool)], owners[str(target.tree / "deps" / f"my_tool-{MY_TOOL}")])

    def test_copies_whose_unit_is_gone_are_orphans(self) -> None:
        target = self.target()
        scan, owners = scanned(target)
        self.assertEqual({Path(path) for path in scan.orphans.entries}, target.orphans)
        self.assertEqual(owners[str(target.stale_unit)].kind, "unit")

    def test_files_nothing_claims_stay(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        claimed = sorted(str(path) for path in target.unclaimed if str(path) in owners)
        self.assertEqual(claimed, [])


class DepInfoTests(SweepCase):
    def test_names_the_copy_in_its_directory(self) -> None:
        target = self.target()
        directory = str(target.tree / "examples")
        self.assertEqual(sweep.dep_info_output(str(target.demo_dep_info), directory), "demo")
        self.assertEqual(sweep.dep_info_output(str(target.gone_dep_info), directory), "gone")

    def test_escaped_space(self) -> None:
        target = self.target()
        _ = dep_info(target.tree / "a b", str(target.tree / "a b").replace(" ", "\\ "))
        self.assertEqual(sweep.dep_info_output(str(target.tree / "a b.d"), str(target.tree)), "a b")

    def test_another_directory_stem_or_no_target_is_not_cargos(self) -> None:
        target = self.target()
        examples = str(target.tree / "examples")
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "elsewhere.d"), examples))
        _ = dep_info(target.tree / "examples" / "renamed", str(target.tree / "examples" / "other"))
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "renamed.d"), examples))
        _ = (target.tree / "examples" / "empty.d").write_text("no target here\n")
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "empty.d"), examples))


class SweepTests(SweepCase):
    def test_orphans_go_within_budget_and_nothing_else_does(self) -> None:
        target = self.target()
        status, output = run_sweep(target, 1 << 40, dry_run=False)
        self.assertEqual(status, 0)
        self.assertIn("within the 1024.0 GiB budget (test)", output)
        self.assertIn(f"removed {len(target.orphans)} orphaned files (", output)
        self.assertNotIn("build units", output)
        self.assertEqual([path for path in target.orphans if path.exists()], [])
        kept = [target.app, target.demo, target.tool, target.my_tool, target.stale_unit, *target.unclaimed]
        self.assertEqual([path for path in kept if not path.exists()], [])

    def test_dry_run_reports_orphans_apart_from_evictions(self) -> None:
        target = self.target()
        scan, _ = scanned(target)
        total = sum(scan.blocks.values())
        orphaned = sweep.freed_by(scan.orphans, scan, dict(scan.links))
        status, output = run_sweep(target, total - orphaned - 1, dry_run=True)
        self.assertEqual(status, 0)
        lines = output.splitlines()
        self.assertTrue(lines[1].startswith(f"lint sweep: would remove {len(target.orphans)} orphaned files ("))
        self.assertTrue(lines[2].startswith("lint sweep: would remove 1 build units and 0 incremental dirs ("))
        self.assertEqual([path for path in target.orphans if not path.exists()], [])
        self.assertTrue(target.app.exists())

    def test_evicting_a_unit_removes_its_copies_and_frees_their_blocks(self) -> None:
        target = self.target()
        scan, _ = scanned(target)
        total = sum(scan.blocks.values())
        orphaned = sweep.freed_by(scan.orphans, scan, dict(scan.links))
        # Removing app's unit, the least recently used, frees its file only
        # because its hard-linked copy in debug/ goes with it.
        status, output = run_sweep(target, total - orphaned - SIZE, dry_run=False)
        self.assertEqual(status, 0)
        self.assertIn("removed 1 build units and 0 incremental dirs", output)
        gone = [
            target.app,
            target.app_dep_info,
            target.tree / "deps" / f"app-{APP}",
            target.tree / ".fingerprint" / f"app-{APP}",
        ]
        self.assertEqual([path for path in gone if path.exists()], [])
        self.assertTrue(target.demo.exists())
        self.assertTrue(target.tool.exists())


CONFIG = """\
# sweep_budget_gib=1 in a comment counts for nothing
[operations]
sweep=on

[sweep]
sweep_budget_gib = 24   # every repo without its own key
sweep_budget_gib.hana=96
sweep_budget_gib.hana=1
sweep_budget_gib.broken=lots
sweep_budget_gib.unset=
"""
# No signing, hooks or identity from the user's own git config.
GIT_ENVIRONMENT = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path) -> None:
    _ = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENVIRONMENT, capture_output=True, check=True)


class BudgetTests(unittest.TestCase):
    def config(self) -> str:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "lint.conf"
        _ = path.write_text(CONFIG)
        return str(path)

    def test_config_reads_as_lint_config_sh_does(self) -> None:
        values = sweep.config_values(self.config())
        self.assertEqual(values["sweep"], "on")
        self.assertEqual(values["sweep_budget_gib"], "24")
        self.assertEqual(values["sweep_budget_gib.hana"], "96")
        self.assertEqual(values["sweep_budget_gib.unset"], "")
        self.assertEqual(sweep.config_values("/nonexistent/lint.conf"), {})

    def test_repo_key_then_default_key_then_built_in(self) -> None:
        config = self.config()
        self.assertEqual(sweep.budget_bytes({}, config, "hana"), (96 * GIB, f"sweep_budget_gib.hana in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "nateroids"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "unset"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, None), (24 * GIB, f"sweep_budget_gib in {config}"))
        missing = "/nonexistent/lint.conf"
        self.assertEqual(
            sweep.budget_bytes({}, missing, "hana"), (24 * GIB, f"the default, no sweep_budget_gib in {missing}")
        )

    def test_environment_overrides_the_config(self) -> None:
        config = self.config()
        environ = {sweep.BUDGET_ENV: "8.5"}
        self.assertEqual(
            sweep.budget_bytes(environ, config, "hana"), (int(8.5 * GIB), "LINT_SWEEP_BUDGET_GIB in the environment")
        )
        self.assertEqual(sweep.budget_bytes({sweep.BUDGET_ENV: ""}, config, "hana")[0], 96 * GIB)

    def test_malformed_value_names_its_source(self) -> None:
        config = self.config()
        self.assertEqual(sweep.budget_bytes({}, config, "broken"), (None, f"sweep_budget_gib.broken in {config}"))
        for raw in ("-1", "inf", "nan", "96GiB"):
            self.assertEqual(sweep.budget_bytes({sweep.BUDGET_ENV: raw}, config, "hana")[0], None, raw)


class RepoNameTests(unittest.TestCase):
    def test_every_worktree_and_subdirectory_names_the_repo(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        repo = base / "hana"
        (repo / "crates").mkdir(parents=True)
        _ = (repo / "README").write_text("readme\n")
        git("init", "-q", "-b", "main", cwd=repo)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "first", cwd=repo)
        git("worktree", "add", "-q", "-b", "trunk", str(base / "tool-based-ui-trunk"), cwd=repo)
        self.assertEqual(sweep.repo_name(str(repo)), "hana")
        self.assertEqual(sweep.repo_name(str(repo / "crates")), "hana")
        self.assertEqual(sweep.repo_name(str(base / "tool-based-ui-trunk")), "hana")
        self.assertIsNone(sweep.repo_name(str(base)))


if __name__ == "__main__":
    _ = unittest.main()
