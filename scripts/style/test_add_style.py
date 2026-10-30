"""Bootstrap a style production in disposable repositories without starting sessions."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

import add_style
import add_unit
import rules
import style_ledger
import unit_plans
import style_rules
from production_units import read_production
from production_units import production_field
from scripts.accounts.accounts import NamedAccount
from scripts.herdr import herdr

SCRIPT = Path(add_style.__file__)
STUB = '''import json
import os
import shlex
import sys
from pathlib import Path

arguments = sys.argv[1:]
name = Path(sys.argv[0]).name
with Path(os.environ["TEST_EVENTS"]).open("a") as events:
    events.write(json.dumps({"command": name, "arguments": arguments}) + "\\n")
state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
if name == "style_sync.py":
    raise SystemExit(0)
elif name == "launch.py":
    doc = Path(arguments[arguments.index("--production") + 1])
    if os.environ.get("TEST_BOOTSTRAP_AT_LAUNCH"):
        Path(os.environ["TEST_BOOTSTRAP_AT_LAUNCH"]).write_text(json.dumps({
            "production": doc.read_text(),
            "plans": {path.name: path.read_text() for path in (doc.parent / "style-production/units").glob("*.md")},
        }))
    destination = next(line for line in doc.read_text().splitlines() if line.startswith("- **Parent production:**"))
    with Path(os.environ["TEST_LAUNCH_DESTINATIONS"]).open("a") as destinations:
        destinations.write(destination + "\\n")
    if os.environ.get("TEST_FAIL_LAUNCH") == "1":
        raise SystemExit(2)
    slug = doc.name.removesuffix("-production.md")
    if os.environ.get("TEST_OPEN_SESSION_EXIT"):
        (Path(os.environ["TEST_LIVE_SESSIONS"]) / slug).touch()
        raise SystemExit(int(os.environ["TEST_OPEN_SESSION_EXIT"]))
    timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / ("showrunner-" + slug) / "conf"
    timer.parent.mkdir(parents=True, exist_ok=True)
    timer.write_text("TARGET=session:stub\\nCHECK=zsh /x/production_check.sh " + shlex.quote(str(doc)) + "\\n")
    parent = state / slug / "parent"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(os.environ["TEST_PARENT_SLUG"] + "\\n")
elif arguments[0] == "detach":
    (state / arguments[1] / "parent").unlink()
elif arguments[0] == "attach":
    parent = state / arguments[1] / "parent"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(arguments[2] + "\\n")
else:
    raise SystemExit(2)
'''
BOOTSTRAP = '''import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(sys.argv[1]).parent))
import add_style

real_run_script = add_style._run_script
def run_script(script, *arguments):
    if script.name == "style_sync.py":
        script = Path(os.environ["TEST_STYLE_SYNC"])
    return real_run_script(script, *arguments)

with mock.patch.object(add_style, "_run_script", side_effect=run_script), mock.patch.object(
    add_style, "herdr_tab_open", side_effect=lambda name, ending:
    (Path(os.environ["TEST_LIVE_SESSIONS"]) / name).exists()
):
    raise SystemExit(add_style.main(sys.argv[2:]))
'''


class ScriptCall(TypedDict):
    command: str
    arguments: list[str]


class BootstrapAtLaunch(TypedDict):
    production: str
    plans: dict[str, str]


@final
class AddStyleTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    checkout: Path = Path()
    parent: Path = Path()
    doc: Path = Path()
    initial_tip: str = ""
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="add-style-")))
        self.repo = self.root / "sample"
        self.checkout = self.root / "sample-style"
        self.doc = self.checkout / "docs/sample-style-production.md"
        self.repo.mkdir()
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Style bootstrap tests")
        _ = self.git("config", "user.email", "style-tests@example.invalid")
        _ = (self.repo / "Cargo.toml").write_text('[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n')
        (self.repo / "src").mkdir()
        _ = (self.repo / "src/lib.rs").write_text("pub fn value() -> u32 { 1 }\n")
        local = self.repo / "docs/style/local.md"
        local.parent.mkdir(parents=True)
        _ = local.write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Local rule\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "-m", "Create sample repository")
        self.initial_tip = self.git("rev-parse", "HEAD")
        rule_root = self.root / "nate_style"
        (rule_root / "rust").mkdir(parents=True)
        for family in add_style.FAMILIES:
            _ = (rule_root / "rust" / f"{family}.md").write_text(
                f"---\ntier: C\nfamily: {family}\ninput_scope: file\n---\n# {family}\n",
            )
        for name in ("launch.py", "showrunners.py", "style_sync.py"):
            _ = (self.root / name).write_text(STUB)
        (self.root / "live-sessions").mkdir()
        self.env = {
            **os.environ,
            "STYLE_HISTORY_NATE_STYLE_DIR": str(rule_root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "ledger"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunners"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "ADD_STYLE_LAUNCHER": str(self.root / "launch.py"),
            "ADD_STYLE_SHOWRUNNERS": str(self.root / "showrunners.py"),
            "TEST_EVENTS": str(self.root / "events.jsonl"),
            "TEST_LAUNCH_DESTINATIONS": str(self.root / "launch-destinations.txt"),
            "TEST_PARENT_SLUG": "parent",
            "TEST_LIVE_SESSIONS": str(self.root / "live-sessions"),
            "TEST_STYLE_SYNC": str(self.root / "style_sync.py"),
        }
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.env)))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_root))
        self.parent = self.parent_doc("parent")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, text=True, capture_output=True,
        ).stdout.strip()

    def parent_doc(self, slug: str, branch: str = "main", *, proposals: bool = True) -> Path:
        path = self.root / f"{slug}-production.md"
        fields = [
            f"# Production — {slug}", "", "## Production Context", "",
            f"- **Repository:** `{self.repo}`",
            f"- **Merge branch:** `{branch}`",
            f"- **Showrunner checkout:** `{self.repo}`",
            f"- **Log:** docs/{slug}-log.md",
            "- **Merge tests:** `sample`, `support` — packages tested on every merge",
        ]
        if proposals:
            fields.append(f"- **Proposals:** `{self.root / (slug + '-issues')}`")
        _ = path.write_text("\n".join([*fields, "", "## Units", "", "| Unit | Plan |", "| --- | --- |", ""]))
        timer = self.root / "notifier" / f"showrunner-{slug}" / "conf"
        timer.parent.mkdir(parents=True, exist_ok=True)
        _ = timer.write_text(f"TARGET=session:{slug}\nCHECK=zsh /x/production_check.sh {shlex.quote(str(path))}\n")
        return path

    def run_bootstrap(self, *arguments: str, parent: Path | None = None, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP, str(SCRIPT), "--production", str(parent or self.parent),
             "--model", "test-model", "--effort", "high", *arguments],
            env=self.env, cwd=self.repo, capture_output=True, text=True, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self) -> list[ScriptCall]:
        path = self.root / "events.jsonl"
        return [cast(ScriptCall, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def resume_call(self) -> ScriptCall:
        return {"command": "style_sync.py", "arguments": [
            "resume", "--repo", str(self.checkout), "--slug", "sample-style",
            "--branch-point", "main", "--style-branch", "style/main",
        ]}

    def sync(self) -> tuple[str, str, str]:
        with closing(sqlite3.connect(self.root / "ledger/sample/ledger.sqlite")) as connection:
            return cast(tuple[str, str, str], connection.execute("SELECT tip,style_branch,synced_at FROM sync").fetchone())

    def add_workspace_packages(self) -> None:
        manifest = self.repo / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[workspace]\nmembers = ["crates/handler", "crates/port"]\n')
        for directory, package in (("handler", "cargo-handler"), ("port", "cargo-port")):
            member = self.repo / "crates" / directory
            (member / "src").mkdir(parents=True)
            _ = (member / "Cargo.toml").write_text(f'[package]\nname = "{package}"\nversion = "0.1.0"\n')
            _ = (member / "src/lib.rs").write_text("pub fn member() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "-m", "Add workspace packages")

    def interrupted_bootstrap_publish(self, path: Path) -> None:
        injection = """import atomic_file
real_replace = atomic_file.os.replace
def interrupted_replace(source, destination):
    if Path(destination) == Path(os.environ["TEST_STOP_FILE"]):
        raise OSError("stopped before publish")
    return real_replace(source, destination)
atomic_file.os.replace = interrupted_replace
"""
        stopped_bootstrap = BOOTSTRAP.replace("real_run_script =", injection + "\nreal_run_script =", 1)
        with mock.patch.dict(self.env, {"TEST_STOP_FILE": str(path)}), mock.patch.dict(
            globals(), {"BOOTSTRAP": stopped_bootstrap}
        ):
            stopped = self.run_bootstrap(success=False)
        self.assertEqual(stopped.returncode, 2, stopped.stderr)
        self.assertIn("stopped before publish", stopped.stderr)
        self.assertFalse(path.exists())
        finished = self.run_bootstrap()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(read_production(self.doc).merge_branch, "style/main")
        self.assertEqual(len(list((self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))), 4)
        self.assertIn("## Units", self.doc.read_text())
        self.assertIn("## Phases", (self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md").read_text())
        self.assertEqual(sum(call["command"] == "launch.py" for call in self.calls()), 1)

    def test_interrupted_first_production_doc_publish_reruns_bootstrap(self) -> None:
        self.interrupted_bootstrap_publish(self.doc)

    def test_interrupted_first_unit_plan_publish_reruns_bootstrap(self) -> None:
        self.interrupted_bootstrap_publish(self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md")

    def test_pilot_rows_and_package_scopes_are_ready_before_the_showrunner_launches(self) -> None:
        self.add_workspace_packages()
        snapshot = self.root / "bootstrap-at-launch.json"
        self.env["TEST_BOOTSTRAP_AT_LAUNCH"] = str(snapshot)

        _ = self.run_bootstrap("--pilot", "modules-unit", "--pilot", "names-unit=cargo-handler,cargo-port",
                               "--pilot", "types-unit=cargo-port")

        launched = cast(BootstrapAtLaunch, json.loads(snapshot.read_text()))
        self.assertEqual(launched["production"], self.doc.read_text())
        self.assertEqual([line.split("|")[1].strip() for line in launched["production"].splitlines()
                          if line.startswith("| ") and "-unit |" in line],
                         ["sample-style-names-unit", "sample-style-types-unit", "sample-style-modules-unit"])
        self.assertEqual(set(launched["plans"]), {f"sample-style-{family}-unit.md" for family in add_style.FAMILIES})
        for unit, scope in (("names", "cargo-handler, cargo-port"), ("types", "cargo-port")):
            lines = launched["plans"][f"sample-style-{unit}-unit.md"].splitlines()
            scope_index = lines.index(f"- **Pilot scope:** {scope}")
            self.assertLess(lines.index("## Delegation Context"), scope_index)
            self.assertTrue(lines[scope_index - 1].startswith("  - `"))
            self.assertLess(scope_index, lines.index("## Phases"))
        for unit in ("values", "modules"):
            self.assertNotIn("**Pilot scope:**", launched["plans"][f"sample-style-{unit}-unit.md"])
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-names-unit.md"], self.repo),
                         unit_plans.PilotCrates(frozenset({"crates/handler", "crates/port"})))
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-types-unit.md"], self.repo),
                         unit_plans.PilotCrates(frozenset({"crates/port"})))
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-modules-unit.md"], self.repo),
                         unit_plans.AllCrates())

    def test_pilot_is_refused_for_an_existing_production_without_changing_it(self) -> None:
        _ = self.run_bootstrap()
        paths = (self.doc, *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: path.read_bytes() for path in paths}, self.calls(), self.sync()
        for extra in ((), ("--check",)):
            result = self.run_bootstrap("--pilot", "names-unit=sample", *extra, success=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), "add_style: --pilot applies only while creating sample-style")
            self.assertEqual(({path: path.read_bytes() for path in paths}, self.calls(), self.sync()), before)

    def test_invalid_pilot_units_packages_and_duplicate_units_refuse_before_writes(self) -> None:
        self.add_workspace_packages()
        for arguments, bad in (
            (("--pilot", "unknown-unit"), "unknown-unit"),
            (("--pilot", "names-unit=absent"), "absent"),
            (("--pilot", "names-unit=crates/handler"), "crates/handler"),
            (("--pilot", "names-unit", "--pilot", "names-unit=sample"), "names-unit"),
            (("--pilot", "names-unit="), "''"),
        ):
            for extra in ((), ("--check",)):
                with self.subTest(arguments=arguments, extra=extra):
                    result = self.run_bootstrap(*arguments, *extra, success=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(bad, result.stderr)
                    self.assertFalse(self.checkout.exists())
                    self.assertFalse((self.root / "ledger").exists())
                    self.assertEqual(self.calls(), [])

    def test_interrupted_pilot_changes_only_unlaunched_rows_and_plan_scopes(self) -> None:
        self.add_workspace_packages()
        self.env["TEST_FAIL_LAUNCH"] = "1"
        self.assertEqual(self.run_bootstrap("--pilot", "names-unit=cargo-handler", "--pilot", "types-unit=cargo-handler",
                                           "--pilot", "values-unit=sample", success=False).returncode, 2)
        launched = self.root / "sample-style-names"
        launched.mkdir()
        names_plan = self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md"
        _ = names_plan.write_text(names_plan.read_text() + "\n### Phase 1 — launched record\n")
        _ = self.doc.write_text(self.doc.read_text().replace("| sample-style-names | — |", "| sample-style-names | 7777 |"))
        names_row = next(line for line in self.doc.read_bytes().splitlines(keepends=True) if line.startswith(b"| sample-style-names-unit |"))
        names_before = names_plan.read_bytes(), names_plan.stat().st_mtime_ns
        types_plan = self.checkout / add_style.PLAN_DIRECTORY / "sample-style-types-unit.md"
        _ = types_plan.write_text(types_plan.read_text() + "\nUnlaunched notes retained.\n")
        log = self.checkout / "docs/sample-style-log.md"
        _ = log.write_text("Interrupted bootstrap notes.\n")
        sync, excluded = self.sync(), (self.repo / ".git/info/exclude").read_bytes()
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap("--pilot", "types-unit=cargo-port", "--pilot", "modules-unit")

        self.assertIn(names_row, self.doc.read_bytes().splitlines(keepends=True))
        self.assertEqual((names_plan.read_bytes(), names_plan.stat().st_mtime_ns), names_before)
        self.assertIn("| sample-style-modules-unit |", self.doc.read_text())
        self.assertNotIn("| sample-style-values-unit |", self.doc.read_text())
        self.assertIn("- **Pilot scope:** cargo-port\n", types_plan.read_text())
        self.assertNotIn("**Pilot scope:** cargo-handler", types_plan.read_text())
        self.assertIn("Unlaunched notes retained.", types_plan.read_text())
        self.assertNotIn("**Pilot scope:**", (self.checkout / add_style.PLAN_DIRECTORY / "sample-style-values-unit.md").read_text())
        self.assertEqual(log.read_text(), "Interrupted bootstrap notes.\n")
        self.assertEqual((self.sync(), (self.repo / ".git/info/exclude").read_bytes()), (sync, excluded))

    def test_check_lists_pilot_units_and_scopes_in_steps_four_and_five_without_writes(self) -> None:
        self.add_workspace_packages()
        result = self.run_bootstrap("--check", "--pilot", "modules-unit", "--pilot", "names-unit=cargo-handler",
                                    "--pilot", "types-unit=cargo-port")
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        for unit in ("sample-style-modules-unit", "sample-style-names-unit", "sample-style-types-unit"):
            self.assertIn(unit, lines[3])
        self.assertIn("sample-style-modules-unit: all crates", lines[4])
        self.assertIn("sample-style-names-unit: cargo-handler", lines[4])
        self.assertIn("sample-style-types-unit: cargo-port", lines[4])
        self.assertNotIn("values-unit", lines[3] + lines[4])
        self.assertFalse(self.checkout.exists())
        self.assertFalse((self.root / "ledger").exists())
        self.assertEqual(self.calls(), [])

    def test_pilot_family_old_id_and_full_id_produce_the_same_check(self) -> None:
        checks = [self.run_bootstrap("--check", "--pilot", f"{name}=sample").stdout
                  for name in ("names", "names-unit", "sample-style-names-unit")]
        self.assertEqual(checks[1:], [checks[0], checks[0]])
        self.assertIn("sample-style-names-unit: sample", checks[0])
        self.assertFalse(self.checkout.exists())
        self.assertFalse((self.root / "ledger").exists())

    def test_pilot_aliases_cannot_select_the_same_family_twice(self) -> None:
        result = self.run_bootstrap("--check", "--pilot", "names=sample",
                                    "--pilot", "sample-style-names-unit=sample", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.calls(), [])

    def test_lookup_from_main_and_linked_worktree_finds_the_same_standing_production(self) -> None:
        _ = self.run_bootstrap()
        main = add_style.standing_style_production(self.repo)
        linked = add_style.standing_style_production(self.checkout)
        self.assertEqual(main, linked)
        self.assertIsInstance(main, add_style.StandingStyleProduction)
        if isinstance(main, add_style.StandingStyleProduction):
            self.assertEqual((main.repository, main.slug, main.checkout, main.doc, main.branch_point),
                             (self.repo, "sample-style", self.checkout, self.doc, "main"))

    def test_lookup_without_a_production_leaves_directories_branches_and_excludes_unchanged(self) -> None:
        before = (sorted(self.root.iterdir()), self.git("branch", "--list"),
                  (self.repo / ".git/info/exclude").read_bytes())
        self.assertEqual(add_style.standing_style_production(self.repo),
                         add_style.NoStyleProduction(self.repo, "sample-style"))
        self.assertEqual((sorted(self.root.iterdir()), self.git("branch", "--list"),
                          (self.repo / ".git/info/exclude").read_bytes()), before)

    def test_bootstrap_creates_excluded_production_plans_and_pins_style_branch(self) -> None:
        _ = self.run_bootstrap()

        self.assertEqual(self.git("rev-parse", "style/main"), self.initial_tip)
        self.assertIn(str(self.checkout), self.git("worktree", "list", "--porcelain"))
        production = read_production(self.doc)
        self.assertEqual((production.slug, production.merge_branch, production.checkout), ("sample-style", "style/main", self.checkout))
        lines = self.doc.read_text().splitlines()
        for field, value in {
            "Kind": "style", "Repository": str(self.repo), "Branch point": "main",
            "Parent production": str(self.parent), "Proposals": str(self.root / "parent-issues"),
            "Log": "docs/sample-style-log.md", "Updates": "on demand", "Push": "git",
        }.items():
            self.assertEqual(production_field(lines, field), value)
        self.assertIn("- **Merge tests:** `sample`, `support` — packages tested on every merge", lines)
        for family in add_style.FAMILIES:
            plan = self.checkout / f"docs/style-production/units/sample-style-{family}-unit.md"
            text = plan.read_text()
            self.assertIn(f"| sample-style-{family}-unit | docs/style-production/units/sample-style-{family}-unit.md | {self.root / ('sample-style-' + family)} | sample-style-{family} |", self.doc.read_text())
            self.assertIn(f"> **Production: sample-style** — unit `sample-style-{family}-unit`", text)
            self.assertIn("> **Standing:** style_plan.py", text)
            self.assertNotIn("**Pilot scope:**", text)
            self.assertIn(f"global:rust/{family}.md", text)
            self.assertIn("~/.claude/docs/style_production.md", text)
            self.assertNotIn("### Phase", text)
            for verb in ("check", "test", "lint"):
                self.assertIn(f"verify.sh {verb} <package>", text)
            ignored = self.git("-C", str(self.checkout), "check-ignore", "--", str(plan), str(self.doc), str(self.checkout / "docs/sample-style-log.md"))
            self.assertIn(str(plan), ignored)
        excluded = (self.repo / ".git/info/exclude").read_text()
        self.assertIn("/docs/style-production/\n", excluded)
        self.assertNotIn("/docs/style/\n", excluded)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git("-C", str(self.checkout), "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.sync()[:2], (self.initial_tip, "refs/heads/style/main"))
        self.assertEqual(self.calls(), [{
            "command": "launch.py",
            "arguments": ["--production", str(self.doc), "--model", "test-model", "--effort", "high"],
        }])

    def test_standing_plans_do_not_become_local_rules_or_hide_real_rules(self) -> None:
        _ = self.run_bootstrap()

        catalog = rules.load_rules(self.checkout)
        self.assertEqual({rule.rule_id for rule in catalog if rule.origin == "local"}, {"local:docs/style/local.md"})
        listing = subprocess.run(
            [sys.executable, str(SCRIPT.with_name("rules.py")), "list", "--repo", str(self.checkout), "--json"],
            check=True, capture_output=True, text=True, env=self.env,
        )
        rows = cast(list[rules.RuleListing], json.loads(listing.stdout))
        self.assertEqual({row["rule_id"] for row in rows}, {rule.rule_id for rule in catalog})
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.set_tip(self.initial_tip, "style/main")
            self.assertIn("local:docs/style/local.md", {row.rule_id for row in ledger.rule_counts(style_ledger.WholeRepository())})

    def test_create_refreshes_an_older_ledger_before_pinning_its_style_branch(self) -> None:
        _ = (self.repo / "src/new.rs").write_text("pub fn new_input() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Advance branch point before creating style production")
        branch_tip = self.git("rev-parse", "main")
        for previous_branch in ("", "style/previous"):
            with self.subTest(previous_branch=previous_branch):
                with style_ledger.open_ledger(self.repo) as ledger:
                    if previous_branch:
                        ledger.set_tip(self.initial_tip, previous_branch)
                    else:
                        ledger.set_tip(self.initial_tip)
                timer = self.root / "notifier/showrunner-sample-style/conf"
                if timer.exists():
                    timer.unlink()
                _ = self.run_bootstrap()
                self.assertEqual(self.sync()[:2], (branch_tip, "refs/heads/style/main"))
                with style_ledger.open_ledger(self.checkout) as ledger:
                    self.assertIn("src/new.rs", {cell.path for cell in ledger.cells()})

    def test_create_retry_with_the_exact_sync_pair_does_not_refresh_its_timestamp(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        failed = self.run_bootstrap(success=False)
        self.assertEqual(failed.returncode, 2)
        before = self.sync()
        self.assertEqual(before[:2], (self.initial_tip, "refs/heads/style/main"))
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap()

        self.assertEqual(self.sync(), before)

    def test_rerun_keeps_plans_ledger_and_excludes_and_does_not_launch_twice(self) -> None:
        _ = self.run_bootstrap()
        plan = self.checkout / "docs/style-production/units/sample-style-names-unit.md"
        _ = plan.write_text(plan.read_text() + "\n### Phase 1 — already assigned\n")
        paths = (plan, self.doc, self.repo / ".git/info/exclude")
        before = {path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}
        sync = self.sync()
        calls = self.calls()

        _ = self.run_bootstrap()

        self.assertEqual({path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual(self.sync(), sync)
        self.assertEqual(self.calls(), [*calls, self.resume_call()])

    def test_existing_production_reattaches_updates_destination_and_runs_resume(self) -> None:
        _ = self.run_bootstrap()
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.handoff_add(self.initial_tip)
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Another checkpoint")
        second = self.git("rev-parse", "HEAD")
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.handoff_add(second)
            ledger.handoff_send(second, "parent")
        caller = self.parent_doc("next")
        original_sync = self.sync()
        original_plans = {path: path.read_text() for path in (self.checkout / add_style.PLAN_DIRECTORY).glob("*.md")}

        result = self.run_bootstrap(parent=caller)

        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls()[-1], self.resume_call())
        with style_ledger.open_ledger(self.checkout) as ledger:
            self.assertEqual({row["checkpoint"]: row["state"] for row in ledger.pending_handoffs()},
                             {self.initial_tip: "pending", second: "sent"})
        fields = self.doc.read_text().splitlines()
        self.assertEqual(production_field(fields, "Parent production"), str(caller))
        self.assertEqual(production_field(fields, "Proposals"), str(self.root / "next-issues"))
        self.assertEqual(self.calls()[1:], [
            {"command": "showrunners.py", "arguments": ["detach", "sample-style"]},
            {"command": "showrunners.py", "arguments": ["attach", "sample-style", "next"]},
            self.resume_call(),
        ])
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text().strip(), "next")
        self.assertEqual(self.sync(), original_sync)
        self.assertEqual({path: path.read_text() for path in original_plans}, original_plans)

    def test_missing_proposals_refuses_before_writes_in_check_and_apply(self) -> None:
        parent = self.parent_doc("missing", proposals=False)
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), (self.repo / ".git/info/exclude").read_text()
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments, parent=parent, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Proposals", result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertFalse(self.checkout.exists())
                self.assertFalse((self.root / "ledger").exists())
                self.assertEqual(self.calls(), [])
                self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), (self.repo / ".git/info/exclude").read_text()), before)

    def test_check_prints_seven_steps_without_creating_state_or_artifacts(self) -> None:
        before = self.git("show-ref"), (self.repo / ".git/info/exclude").read_text()

        result = self.run_bootstrap("--check")

        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual([line.split(".", 1)[0] for line in lines], [str(number) for number in range(1, 8)])
        for expected in ("style/main", "docs/style-production/units", "info/exclude", "add_showrunner.py"):
            self.assertIn(expected, result.stdout)
        self.assertFalse(self.checkout.exists())
        self.assertFalse((self.root / "ledger").exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.git("show-ref"), (self.repo / ".git/info/exclude").read_text()), before)

    def test_check_existing_production_describes_attachment_and_preserves_all_state(self) -> None:
        _ = self.run_bootstrap()
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Advance parent beyond synced tip")
        caller = self.parent_doc("next")
        paths = (self.doc, self.repo / ".git/info/exclude", *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}
        refs, worktrees, sync, calls = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.sync(), self.calls()
        parent = (self.root / "showrunners/sample-style/parent").read_text()

        result = self.run_bootstrap("--check", parent=caller)

        self.assertEqual(result.stdout.splitlines(), [
            f"1. read {caller}; repository {self.repo}; Proposals {self.root / 'next-issues'}",
            "2. attach standing sample-style under next; branch point main",
            *(f"{number}. skipped: sample-style already exists" for number in range(3, 7)),
            "7. update Parent production and Proposals, run style_sync.py resume; synced tip unchanged",
        ])
        self.assertEqual({path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.sync(), self.calls()),
                         (refs, worktrees, sync, calls))
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text(), parent)

    def test_unready_live_session_requires_timer_before_retry_can_attach_without_relaunching(self) -> None:
        self.env["TEST_OPEN_SESSION_EXIT"] = "3"

        result = self.run_bootstrap(success=False)

        guidance = (
            "look at it with herdr session attach agents, workspace sample-style, tab sample-style; "
            "once it has its update timer, rerun add_style, which attaches it; "
            "if it never gets one, end that session and rerun"
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("add_style: sample-style was left open by add_showrunner.py (exit 3); " + guidance, result.stderr)
        self.assertNotIn("rerun add_style to continue", result.stderr)
        self.assertTrue((self.root / "live-sessions/sample-style").exists())
        calls, sync = self.calls(), self.sync()

        retry = self.run_bootstrap(success=False)

        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn(guidance, retry.stderr)
        self.assertEqual((self.calls(), self.sync()), (calls, sync))
        timer = self.root / "notifier/showrunner-sample-style/conf"
        timer.parent.mkdir(parents=True)
        _ = timer.write_text(f"TARGET=session:stub\nCHECK=zsh /x/production_check.sh {shlex.quote(str(self.doc))}\n")

        _ = self.run_bootstrap()

        self.assertEqual(self.calls(), [*calls, {
            "command": "showrunners.py", "arguments": ["attach", "sample-style", "parent"],
        }, self.resume_call()])
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text().strip(), "parent")
        self.assertEqual(self.sync(), sync)

    def test_bootstrapped_standing_plan_passes_real_unit_preflight(self) -> None:
        _ = self.run_bootstrap()
        plan = add_style.PLAN_DIRECTORY / "sample-style-names-unit.md"
        request = add_unit.RequestedUnitLaunch(
            production=read_production(self.doc), requested_name="sample-style-names", unit="sample-style-names-unit",
            branch="sample-style-names", worktree=self.root / "sample-style-names",
            plan=add_unit.PlanGiven(plan), port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(),
            session=add_unit.NewSession(), launch_kind=add_unit.NewWorkLaunch(), timeout=1.0,
        )
        before = self.git("status", "--porcelain", "--untracked-files=all"), self.git("show-ref"), self.doc.read_text()
        agent_config = self.root / "agents.conf"
        _ = agent_config.write_text(
            "[assignments]\nproduction=claude\n\n[production.claude]\ndirector=opus:xhigh\n\n"
            + "[claude.agents]\nopus=low,medium,high,xhigh\n",
        )
        catalog_sync = self.root / "catalog-synced"
        catalog_sync.touch()

        with (
            mock.patch.dict(os.environ, {
                "AGENTS_CONFIG_FILE": str(agent_config), "CODEX_CATALOG_SYNC_STATE_FILE": str(catalog_sync),
                "CODEX_CONFIG_FILE": str(self.root / "codex.toml"), "CODEX_MODELS_CACHE_FILE": str(self.root / "models.json"),
            }),
            mock.patch.object(herdr, "claude_trust", return_value=herdr.Trusted(
                self.repo, self.root / "account/.claude.json", self.repo)),
            mock.patch.object(add_unit, "remote_head", return_value=""),
        ):
            ready = add_unit.preflight(request, NamedAccount("test", self.root / "account"))

        self.assertEqual(ready.launch.plan_path, plan)
        self.assertEqual(ready.launch.identity.unit, "sample-style-names-unit")
        self.assertIsInstance(ready.row, add_unit.ExistingUnitRow)
        self.assertEqual((self.git("status", "--porcelain", "--untracked-files=all"), self.git("show-ref"), self.doc.read_text()), before)
        self.assertFalse(request.worktree.exists())

    def test_second_branch_point_is_refused_without_creating_or_reattaching_anything(self) -> None:
        _ = self.run_bootstrap()
        _ = self.git("branch", "init/catalyst")
        parent = self.parent_doc("other", "init/catalyst")
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.doc.read_text(), self.sync(), self.calls()
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments, parent=parent, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(), "add_style: sample already has style production sample-style on branch point main; one branch point per repository")
                self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.doc.read_text(), self.sync(), self.calls()), before)

    def test_slash_branch_point_uses_main_checkout_location_even_when_parent_runs_in_a_worktree(self) -> None:
        _ = self.git("branch", "init/catalyst")
        product_checkout = self.root / "product-checkout"
        _ = self.git("worktree", "add", "-q", str(product_checkout), "init/catalyst")
        parent = self.parent_doc("nested", "init/catalyst")
        _ = parent.write_text(parent.read_text().replace(str(self.repo), str(product_checkout)))

        _ = self.run_bootstrap(parent=parent)

        self.assertEqual(self.git("rev-parse", "style/init-catalyst"), self.initial_tip)
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Repository"), str(self.repo))
        self.assertEqual(self.sync()[:2], (self.initial_tip, "refs/heads/style/init-catalyst"))
        self.assertFalse((self.root / "product-checkout-style").exists())

    def test_retry_after_failed_launch_preserves_open_plan_and_keeps_ledger_at_style_branch_commit(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        refused = self.run_bootstrap(success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("launch.py exited 2", refused.stderr)
        plan = self.checkout / "docs/style-production/units/sample-style-values-unit.md"
        _ = plan.write_text(plan.read_text() + "\n### Phase 1 — resumed work\n")
        original_plan = plan.read_text()
        _ = (self.repo / "src/new.rs").write_text("pub fn added_after_launch() {}\n")
        _ = self.git("add", "src/new.rs")
        _ = self.git("commit", "-q", "-m", "Advance branch point before retry")
        branch_tip = self.git("rev-parse", "main")
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap()

        self.assertEqual(plan.read_text(), original_plan)
        style_tip = self.git("rev-parse", "refs/heads/style/main")
        self.assertEqual(style_tip, self.initial_tip)
        self.assertNotEqual(style_tip, branch_tip)
        self.assertEqual(self.sync()[:2], (style_tip, "refs/heads/style/main"))
        with style_ledger.open_ledger(self.checkout) as ledger:
            self.assertNotIn("src/new.rs", {cell.path for cell in ledger.cells()})
        self.assertEqual([call["command"] for call in self.calls()], ["launch.py", "launch.py"])
        self.assertEqual(self.git("worktree", "list", "--porcelain").count(f"worktree {self.checkout}"), 1)

    def test_retry_completes_worktree_left_before_production_doc_was_written(self) -> None:
        _ = self.git("worktree", "add", "-q", "-b", "style/main", str(self.checkout), self.initial_tip)

        _ = self.run_bootstrap()

        self.assertTrue(self.doc.exists())
        self.assertEqual(len(tuple((self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))), 4)
        self.assertEqual(self.sync()[:2], (self.initial_tip, "refs/heads/style/main"))
        self.assertEqual(len(self.calls()), 1)

    def test_interrupted_bootstrap_rewrites_destination_for_the_next_parent_only(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        failed = self.run_bootstrap(success=False)
        self.assertEqual(failed.returncode, 2)
        plans = {path: path.read_bytes() for path in (self.checkout / add_style.PLAN_DIRECTORY).glob("*.md")}
        sync, excludes = self.sync(), (self.repo / ".git/info/exclude").read_bytes()
        caller = self.parent_doc("next")
        self.env["TEST_FAIL_LAUNCH"] = "0"
        self.env["TEST_PARENT_SLUG"] = "next"
        _ = self.run_bootstrap(parent=caller)
        fields = self.doc.read_text().splitlines()
        self.assertEqual(production_field(fields, "Parent production"), str(caller))
        self.assertEqual(production_field(fields, "Proposals"), str(self.root / "next-issues"))
        self.assertEqual({path: path.read_bytes() for path in plans}, plans)
        self.assertEqual((self.sync(), (self.repo / ".git/info/exclude").read_bytes()), (sync, excludes))
        launches = [call for call in self.calls() if call["command"] == "launch.py"]
        self.assertEqual(len(launches), 2)
        self.assertIn(str(self.doc), launches[-1]["arguments"])
        destinations = (self.root / "launch-destinations.txt").read_text().splitlines()
        self.assertEqual(len(destinations), 2)
        self.assertEqual(production_field([destinations[0]], "Parent production"), str(self.parent))
        self.assertEqual(production_field([destinations[1]], "Parent production"), str(caller))
        self.assertNotIn(self.resume_call(), self.calls())

    def test_check_interrupted_bootstrap_describes_destination_repair_without_writes(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        self.assertEqual(self.run_bootstrap(success=False).returncode, 2)
        caller = self.parent_doc("next")
        paths = (self.doc, self.repo / ".git/info/exclude", *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        calls, sync = self.calls(), self.sync()
        result = self.run_bootstrap("--check", parent=caller)
        self.assertEqual(result.stdout.splitlines()[3],
                         f"4. set Parent production and Proposals in existing production doc {self.doc}")
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual((self.calls(), self.sync()), (calls, sync))
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Parent production"), str(self.parent))


if __name__ == "__main__":
    _ = unittest.main()
