"""Private Git/worktree copies and real CLI entry points for style-planner tests."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import ModuleType
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_footprint
import style_plan
import style_proposals
import proposal_issue

import berth_board
import fake_fmt
import mechanical_classes
import rules
import style_ledger
import style_rules
from scripts.production.cli_fixture import invoke_cli


REAL_RUN = subprocess.run


class StylePlanFixture(unittest.TestCase):
    root: Path = Path()

    repo: Path = Path()

    style: Path = Path()

    unit: Path = Path()

    plan: Path = Path()

    doc: Path = Path()

    rule_dir: Path = Path()

    tip: str = ""

    pending_judge_tickets: frozenset[str] = frozenset()

    sources: tuple[str, ...] = tuple(f"crates/{crate}/src/{name}.rs" for crate in ("alpha", "beta") for name in ("lib", "other"))

    template_repo: Path = Path()

    template_tip: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.template_repo = Path(cls.enterClassContext(tempfile.TemporaryDirectory())) / "sample"
        cls.template_repo.mkdir()
        fixture = cls(methodName="runTest")
        fixture.repo = cls.template_repo
        # Match setUp's isolated Git configuration while building the class template.
        with mock.patch.dict(os.environ, {"HOME": str(cls.template_repo.parent)}):
            _ = fixture.git("init", "-q", "--initial-branch=main")
            _ = fixture.git("config", "user.name", "Planner tests")
            _ = fixture.git("config", "user.email", "planner@example.invalid")
            fixture.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n')
            for crate in ("alpha", "beta"):
                fixture.write(f"crates/{crate}/Cargo.toml",
                           f'[package]\nname = "{crate}"\nversion = "0.1.0"\nedition = "2021"\n')
            for path in fixture.sources:
                fixture.write(path, "pub fn value() {}\n")
            cls.template_tip = fixture.commit()

        cls.fixture_root = cls.template_repo.parent / "case"
        cls.snapshot = cls.template_repo.parent / "snapshot"
        fixture = cls(methodName="runTest")
        try:
            fixture.build_fixture()
            _ = shutil.copytree(cls.fixture_root, cls.snapshot, symlinks=True)
        finally:
            fixture.doCleanups()
        shutil.rmtree(cls.fixture_root)

    fixture_root: ClassVar[Path] = Path()
    snapshot: ClassVar[Path] = Path()

    @override
    def setUp(self) -> None:
        _ = shutil.copytree(self.snapshot, self.fixture_root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.configure_fixture()
        # Keep one gate case on the executable path; the others retain the real parser and handlers in-process.
        if self._testMethodName != "test_structure_file_cells_gate_and_checkpoint_require_the_last_file_verdict":
            _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.run_command))

    def configure_fixture(self) -> None:
        listed = mechanical_classes.classes()
        _ = self.enterContext(mock.patch.object(mechanical_classes, "classes", return_value=listed))
        self.root = self.fixture_root
        self.pending_judge_tickets = frozenset()
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.unit = self.root / "sample-style-names"
        self.rule_dir = self.root / "nate_style/rust"
        self.doc = self.style / "docs/sample-style-production.md"
        self.plan = self.unit / "docs/style-production/units/names-unit.md"
        self.tip = self.template_tip
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.rule_dir.parent),
            fake_fmt.STATE: str(self.root / "fmt"),
            "PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.rule_dir.parent))
        _ = self.enterContext(mock.patch.object(berth_board, "read_board", return_value=self.board()))
        _ = self.enterContext(mock.patch.object(style_proposals, "renumber"))

    def run_command(self, command: list[str], *, capture_output: bool = False, text: bool = False,
                    check: bool = False, cwd: Path | str | None = None, env: dict[str, str] | None = None,
                    stdin: int | None = None, stdout: int | None = None, stderr: int | None = None,
                    encoding: str | None = None, errors: str | None = None,
                    input: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ) if env is None else env
        directory = Path.cwd() if cwd is None else Path(cwd)
        module = None
        arguments = command[2:]
        if command[0] == sys.executable and len(command) > 1:
            module = next((item for item in (style_plan, style_ledger, style_footprint, proposal_issue)
                           if Path(command[1]) == Path(item.__file__ or "")), None)
        executable = Path(shutil.which(command[0], path=environment.get("PATH")) or command[0])
        if executable.parent == self.root / "bin" and executable.name in fake_fmt.NAMES:
            module = ModuleType("style_plan_formatter_fixture")
            module.__file__ = str(executable)
            arguments = command[1:]

            def main() -> int:
                exec(compile(executable.read_text(), str(executable), "exec"), {**vars(module), "__name__": "__main__"})
                return 0
        elif module is style_plan:
            def main() -> int:
                return style_plan.main(sys.argv[1:])
        elif module is style_footprint:
            def main() -> int:
                return style_footprint.main()
        elif module is style_ledger:
            def main() -> int:
                style_ledger.main()
                return 0
        elif module is proposal_issue:
            def main() -> int:
                return proposal_issue.main(sys.argv[1:])
        else:
            return REAL_RUN(command, capture_output=capture_output, text=text, check=check, cwd=cwd, env=env,
                            stdin=stdin, stdout=stdout, stderr=stderr, encoding=encoding, errors=errors,
                            input=input, timeout=timeout)
        result = invoke_cli(cast(ModuleType, module), main, arguments, environment, directory)
        if not capture_output:
            if stdout != subprocess.DEVNULL:
                print(result.stdout, end="")
            if stderr != subprocess.DEVNULL:
                print(result.stderr, end="", file=sys.stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result

    def build_fixture(self) -> None:
        _ = shutil.copytree(self.template_repo, self.fixture_root / "sample", symlinks=True)
        self.configure_fixture()
        self.rule_dir.mkdir(parents=True)
        fake_fmt.install(self.root / "bin", self.root / "fmt")
        _ = self.git("worktree", "add", "-q", "-b", "style/main", str(self.style))
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-names", str(self.unit))
        _ = (self.repo / ".git/info/exclude").write_text("/docs/style-production/\n/docs/*production.md\n")
        self.doc = self.style / "docs/sample-style-production.md"
        parent = self.repo / "docs/product-production.md"
        parent.parent.mkdir(parents=True)
        _ = parent.write_text(self.production("main", self.repo, ""))
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production("style/main", self.style, str(parent)))
        self.plan = self.unit / "docs/style-production/units/names-unit.md"
        self.plan.parent.mkdir(parents=True)
        _ = self.plan.write_text(self.plan_header())
        showrunner_plan = self.style / "docs/style-production/units/names-unit.md"
        showrunner_plan.parent.mkdir(parents=True)
        _ = showrunner_plan.write_text(self.plan_header() + "Showrunner copy differs.\n")

    def production(self, branch: str, checkout: Path, parent: str) -> str:
        return "\n".join([
            "# Production", f"- **Merge branch:** {branch}", f"- **Showrunner checkout:** {checkout}",
            "- **Log:** docs/log.md", "- **Branch point:** main", f"- **Parent production:** {parent}", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |",
            *([f"| names-unit | docs/style-production/units/names-unit.md | {self.unit} | demo-style-names |",
               f"| modules-unit | docs/style-production/units/modules-unit.md | {self.root / 'modules'} | demo-style-modules |"] if parent else []), "",
        ])

    def plan_header(self) -> str:
        return "\n".join([
            "# Standing names", f"> **Production: sample-style** — unit `names-unit`; production doc `{self.doc}`",
            "> **Standing:** style_plan.py", "", "## Delegation Context", "",
            "- **Project:** sample", "- **Test:** verify.sh test <package>", "", "## Phases", "",
        ])

    def board(self, *blocked: str) -> berth_board.Board:
        return berth_board.Board(tuple(
            berth_board.Reservation(str(index), "refs/heads/main", self.repo, "product", (path,), "active", "blocking")
            for index, path in enumerate(blocked)
        ))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)

    def commit(self) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def rule(self, name: str, metadata: str = "tier: C\nfamily: names\ninput_scope: file\n") -> str:
        _ = (self.rule_dir / (name + ".md")).write_text(f"---\n{metadata}---\n# {name}\nOriginal instructions.\n")
        return "global:rust/" + name + ".md"

    def ledger(self) -> style_ledger.Ledger:
        ledger = style_ledger.open_ledger(self.repo)
        self.addCleanup(ledger.close)
        return ledger

    def sync(self) -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")

    def candidates(self, family: rules.Family = "names", blocked: tuple[str, ...] = ()) -> tuple[style_plan.TicketSelection, ...]:
        self.finish_judging()
        ledger = self.ledger()
        board = self.board(*blocked)
        busy = style_plan.busy_files(self.repo, self.doc, ledger, family + "-unit", board)
        return style_plan.candidate_tickets(self.repo, ledger, busy, family, base=self.base(family))

    def base(self, family: rules.Family = "names") -> style_plan.UnitBase:
        return style_plan.unit_base(self.repo, f"demo-style-{family}")

    def modules_unit(self) -> Path:
        """Add the modules unit's worktree and branch at the main tip, and return its plan."""
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        return plan

    def unit_takes_tip(self) -> None:
        """The style branch takes the synced tip, and the names unit merges the style branch."""
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)

    def run_next(self) -> tuple[int, str, str]:
        self.finish_judging()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = style_plan.main(["next-phase", "--unit", "names-unit", "--plan", str(self.plan), "--repo", str(self.repo)])
        return code, "".join(line for line in output.getvalue().splitlines(keepends=True)
                             if not line.startswith("no available work: ")), errors.getvalue()


    def record(self, rule: str, path: str, commit: str, ticket: str = "outside") -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule.removesuffix("#uncovered"))
            ledger.record(rule=rule, path=path, commit=commit, verdict="clean", detail="", ticket=ticket, worker="test", rule_blob=blob,
                          input_scope=style_ledger.DeclaredInputScope.RULE)

    def held_proposal(self, rule: str, path: str) -> Path:
        issue = self.root / "proposal.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            finding = ledger.hold(rule, path, 1, "held defect", "restructure", "before", "after", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
            ledger.record(rule=rule, path=path, commit=self.tip, verdict="held", detail="", ticket="outside",
                          worker="test", rule_blob=blob, input_scope=style_ledger.DeclaredInputScope.RULE)
        _ = issue.write_text(f"---\nstatus: open\nstyle_findings: [{finding.finding_id}]\n---\n")
        return issue

    def count_tickets(self) -> int:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT COUNT(*) FROM tickets").fetchall())
        return rows[0][0]

    def types_unit(self) -> Path:
        """Add the types unit's worktree, branch and Units row at the main tip, and return its plan."""
        types = self.root / "types"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-types", str(types))
        plan = types / "docs/style-production/units/types-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header().replace("names-unit", "types-unit"))
        _ = self.doc.write_text(self.doc.read_text() +
                                f"| types-unit | docs/style-production/units/types-unit.md | {types} | demo-style-types |\n")
        return plan

    def names_files(self) -> set[str]:
        return {path for ticket in self.candidates("names") for path in ticket.files}

    def prepare_binary_crate(self) -> None:
        (self.repo / self.sources[0]).unlink()
        self.write("crates/alpha/src/main.rs", "fn main() {}\n")
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()

    def held_cell_with_final_findings(self) -> tuple[str, tuple[style_plan.TicketSelection, ...]]:
        """Hold crate alpha's lib.rs under an open proposal, then change only another file of its crate."""
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        return read, (style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),)

    def hold_more(self, read: str, *states: str) -> None:
        """Add one finding to crate alpha's lib.rs in each state."""
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == read)
        for number, state in enumerate(states, len(ledger.held_findings(style_ledger.AllHeldFindings())) + 1):
            finding = ledger.hold(read, self.sources[0], number, f"defect {number}", "rename", "before", "after", blob, "outside")
            if state == "unfiled":
                continue
            ledger.file_held(finding.finding_id, self.root / "proposal.md")
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE held_findings SET state=?,withdrawn_by=? WHERE finding_id=?",
                                       (state, int(state == "withdrawn"), finding.finding_id))
                connection.commit()

    def judged_at_unit_tip_then_style_moves(self) -> tuple[str, str]:
        """Judge crate alpha's lib.rs at the unit tip, merge that into the style branch, then change crate alpha there.

        It returns the rule and the unit tip.
        """
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        _ = (self.unit / self.sources[0]).write_text("pub fn judged() {}\n")
        _ = self.git("commit", "-qam", "Ticket work", cwd=self.unit)
        unit_tip = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(read, self.sources[0], unit_tip)
        _ = self.git("merge", "-q", "--ff-only", "demo-style-names", cwd=self.style)
        self.assertEqual(self.cell_state(read, self.sources[0]), "awaiting merge")
        _ = (self.style / self.sources[1]).write_text("pub fn other_unit() {}\n")
        _ = self.git("commit", "-qam", "Another unit's work in the same crate", cwd=self.style)
        return read, unit_tip

    def cell_state(self, rule: str, path: str) -> style_ledger.CellState:
        return next(cell.state for cell in self.ledger().cells(rule) if cell.path == path)

    def judged_on_all_sources_then_main_changes_crate_alpha(self) -> str:
        """Judge every source at the unit tip with a crate-scope rule, then change crate alpha on main and sync."""
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources:
            self.record(read, path, self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        return read

    def sync_wait_rows(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            return cast(list[tuple[str, str]], connection.execute(
                "SELECT unit,since FROM sync_waits ORDER BY since,unit",
            ).fetchall())

    def seed_sync_wait(self) -> None:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("INSERT OR IGNORE INTO sync_waits (unit,since,token) VALUES ('names-unit','2026-01-01T00:00:00+00:00','seed-wait')")
            connection.commit()

    def approved_decision(self, rule: str, files: tuple[str, ...], kind: style_ledger.FindingKind) -> Path:
        folder = self.root / "issues"
        folder.mkdir(exist_ok=True)
        issue = folder / "decision.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            for path in files:
                finding = ledger.hold(rule, path, 1, "change", kind, "Outcome::Old", "Outcome::New", blob, "outside")
                ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        return issue

    def commit_to_every_checkout(self) -> None:
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()

    def finish_judging(self) -> None:
        """Drop fixture judge tickets after their final writes and before selecting work."""
        if not self.pending_judge_tickets:
            return
        ledger = self.ledger()
        for ticket in sorted(self.pending_judge_tickets):
            ledger.ticket_drop(ticket)
            self.pending_judge_tickets -= {ticket}

    def borrowing_decision(self, rule: str, family: rules.Family, path: str, count: int = 1) -> Path:
        folder = self.root / "borrow-issues"
        folder.mkdir(exist_ok=True)
        issue = folder / (family + ".md")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ledger = self.ledger()
        judge = f"judge-{family}-1"
        ledger.ticket_add(judge, f"judge-{family}", "read", family, "crates/alpha", [path], [rule])
        ledger.ticket_assign(judge, "style/main")
        _ = ledger.ticket_base(judge)
        for index in range(count):
            finding = ledger.hold(rule, path, 1, f"change {index}", "type", "u32", "u64", blob, judge)
            ledger.file_held(finding.finding_id, issue)
        self.pending_judge_tickets |= {judge}
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nplan: approved\n"
                             + f"unit: {family}-unit\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        if "**Proposals:**" not in self.doc.read_text():
            _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        return issue

    def judged_findings(self, rule: str, sites: tuple[tuple[str, int, str], ...], *, kind: style_ledger.FindingKind = "type",
                        before: str = "before", after: str = "after") -> tuple[Path, tuple[style_ledger.HeldFinding, ...]]:
        """Files findings at sites in an approved decision, leaving its judge open for further writes.

        They are type findings unless named.
        """
        issue = self.approved_decision(rule, (), "type")
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "read", "names", "crates/alpha", sorted({site[0] for site in sites}), [rule])
        ledger.ticket_assign("modules-unit-1", "style/main")
        _ = ledger.ticket_base("modules-unit-1")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        findings = tuple(ledger.hold(rule, path, line, text, kind, before, after, blob, "modules-unit-1")
                         for path, line, text in sites)
        for finding in findings:
            ledger.file_held(finding.finding_id, issue)
        self.pending_judge_tickets |= {"modules-unit-1"}
        return issue, findings

    def commit_files(self, files: dict[str, str | bytes]) -> None:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.commit_to_every_checkout()

    def refill_after_site_merge(self, before: dict[str, str | bytes], after: dict[str, str | bytes]) -> tuple[str, style_ledger.HeldState]:
        """Judges a type finding on the first line of the first source at before, merges after and refills.

        Returns the refill output and the finding's state.
        """
        rule = self.rule("read")
        self.commit_files(before)
        site = self.sources[0]
        issue, (finding,) = self.judged_findings(rule, ((site, 1, (self.repo / site).read_text().splitlines()[0]),))
        self.commit_files(after)
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        return output, next(held.state for held in self.ledger().proposal_findings(issue) if held.finding_id == finding.finding_id)

    def write_generated_body(self, issue: Path) -> None:
        _ = issue.write_text(issue.read_text().replace("\n---\n", '\nproject: "[[sample]]"\n---\n', 1)
                             + f"{proposal_issue.GENERATED_START}\nParent: parent\n{proposal_issue.GENERATED_END}\n")

    def refill_after_declaration_move(self, after: dict[str, str | bytes], *,
                                      generated_body: bool = True) -> tuple[Path, style_ledger.HeldFinding, str]:
        """Files a type finding on a field of struct Parent in the first source, then removes that file, commits after
        and refills. With a generated body the declaration step reads the finding; without one it skips the decision.
        """
        rule = self.rule("read")
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(rule, ((site, 2, "old: u32,"),))
        if generated_body:
            self.write_generated_body(issue)
        _ = self.git("rm", "-q", site)
        self.commit_files(after)
        return issue, finding, self.refill_decisions()

    def refill_decisions(self) -> str:
        """Refills with the proposal commands stubbed and a GitHub root for the rule links a decision refresh writes."""
        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(style_proposals, "github_rule_root", return_value=style_proposals.GitHubRuleRoot("example/nate_style", "main"))):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        return output

    def approved_then_changed(self) -> tuple[Path, style_ledger.HeldFinding, str]:
        """A types unit runs. A names finding on the first source, judged by its ticket, is approved; a merge then
        changes its file, and the decisions refill."""
        _ = self.types_unit()
        rule = self.rule("read")
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(rule, ((site, 2, "old: u32,"),))
        self.record(rule, site, self.tip, "modules-unit-1")
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n\npub struct Typed;\n"})
        output = self.refill_decisions()
        # The merge made the finding's cell stale, so the refill gives its file to a ticket that judges it again.
        ledger = self.ledger()
        reading = ledger.ticket_open("names-unit")[0]
        self.assertEqual(reading["kind"], "read")
        self.assertIn(site, reading["files"])
        ledger.ticket_drop(reading["id"])
        return issue, finding, output

    def rejudge(self, finding: style_ledger.HeldFinding, *, holds: bool) -> None:
        """A later read ticket judges the finding's changed file, and holds its change again or records it clean."""
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-2", "modules-unit", "read", "names", "crates/alpha", [finding.path], [finding.rule])
        ledger.ticket_assign("modules-unit-2", "style/main")
        _ = ledger.ticket_base("modules-unit-2")
        if holds:
            _ = ledger.hold(finding.rule, finding.path, finding.line, finding.text, finding.kind, finding.before,
                            finding.after, finding.rule_blob, "modules-unit-2")
        else:
            self.record(finding.rule, finding.path, self.tip, "modules-unit-2")
        ledger.ticket_drop("modules-unit-2")

    def refill_closing(self) -> tuple[int, str, str, int]:
        """Run refill with only the real close command, so a decision it closes is closed on disk.

        The last value is the count of renumber calls.
        """
        original = style_proposals.proposal_command

        def command(*args: str) -> None:
            if args[0] == "close":
                original(*args)
        with (mock.patch.object(style_proposals, "proposal_command", side_effect=command),
              mock.patch.object(style_proposals, "renumber") as renumber):
            return *self.run_next(), renumber.call_count

    def file_rename_decision(self, rule: str, renames: tuple[tuple[str, str], ...],
                             files: tuple[tuple[str, str], ...] = ()) -> Path:
        """An approved decision of file rename findings, each a path and its `Rename <a> to <b>` after. Files replace or
        add files at the synced tip."""
        self.write("crates/alpha/src/lib.rs", "mod gauge;\nmod probe;\nmod render;\n")
        self.write("crates/alpha/src/gauge.rs", "pub fn level() {}\n")
        self.write("crates/alpha/src/probe.rs", "pub fn sample() {}\n")
        self.write("crates/alpha/src/render.rs", "pub fn draw() { crate::probe::sample(); }\n")
        self.write(self.sources[1], "pub fn probe() {}\n")
        for path, text in files:
            self.write(path, text)
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, (), "rename")
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            for path, after in renames:
                finding = ledger.hold(rule, path, 1, "rename", "rename", "module", after, blob, "outside")
                ledger.file_held(finding.finding_id, issue)
        return issue

    def assert_rename_waits_for_an_uncertain_path(self, path: str, text: str) -> None:
        """An approved rename of probe.rs, where the file at path has text with a `path` attribute the rename gate cannot
        read, counts that file as edited and waits while no file is busy."""
        issue = self.file_rename_decision(self.rule("read"), (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),),
                                          ((path, text),))
        finding = self.ledger().proposal_findings(issue)[0]
        self.assertIn(path, style_plan.EditedFiles(self.repo, self.ledger(), style_plan.NoUnitBranch()).of(finding))
        output = self.refill_with_busy()
        self.assertIn(f"waiting {issue}: {finding.path} {path} has a path attribute the rename gate cannot read (1 finding)\n",
                      output)
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def owned_module_decision(self, path: str, owner: str, declaration: str,
                              kind: style_ledger.FindingKind = "restructure") -> tuple[Path, str]:
        """An approved decision of one finding on the module file at path, whose owner file starts with declaration at
        the synced tip, and the finding's id."""
        self.write("crates/alpha/src/lib.rs", "mod census;\nmod terminal;\npub fn value() {}\n")
        self.write(owner, declaration + "pub fn open() {}\n")
        self.write(path, "#[test]\nfn opens() { super::open(); }\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(self.rule("read"), (path,), kind)
        return issue, self.ledger().proposal_findings(issue)[0].finding_id

    def refill_with_busy(self, *paths: str) -> str:
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(*paths)),
              mock.patch.object(style_proposals, "proposal_command")):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        return output

    def approved_reference_ticket(
        self, pairs: tuple[tuple[str, str], ...] = (("Outcome::OldName", "Outcome::NewName"),),
        kinds: tuple[style_ledger.FindingKind, ...] = ("rename",), rule_name: str = "approved-reference",
    ) -> str:
        rule = self.rule(rule_name)
        issue = self.root / "reference-approval.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            ids: list[str] = []
            for index, (before, after) in enumerate(pairs):
                finding = ledger.hold(rule, self.sources[0], index + 1, "rename", kinds[index % len(kinds)],
                                      before, after, blob, "outside")
                ledger.file_held(finding.finding_id, issue)
                ids.append(finding.finding_id)
            _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                                 + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha",
                              [self.sources[0]], [rule], proposal=str(issue), finding_ids=tuple(ids))
        # The gate passes only when the finding file declares each checkable rename's new name.
        declared: dict[str, list[str]] = {}
        for index, (before, after) in enumerate(pairs):
            rename = approved_renames.approved_rename(kinds[index % len(kinds)], before, after)
            if isinstance(rename, approved_renames.ApprovedRename):
                declared.setdefault(rename.parent, []).append(rename.new)
        if declared:
            self.write(self.sources[0], "".join(f"enum {parent} {{ {', '.join(names)} }}\n"
                                                for parent, names in declared.items()))
        return "names-unit-1"

    def approved_binding_ticket(self, source: str, pairs: tuple[tuple[int, str, str], ...],
                                save_base: bool = False) -> tuple[str, tuple[str, ...]]:
        self.write(self.sources[0], source)
        _ = self.commit()
        rule = self.rule("prefer-type-named-fields-and-bindings")
        issue = self.root / "binding-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids: list[str] = []
        for line, old, new in pairs:
            finding = ledger.hold(rule, self.sources[0], line, source.splitlines()[line - 1], "rename",
                                  old, new, blob, "outside")
            ledger.file_held(finding.finding_id, issue)
            ids.append(finding.finding_id)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ticket = "names-unit-1"
        ledger.ticket_add(ticket, "names-unit", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=tuple(ids))
        if save_base:
            ledger.ticket_assign(ticket, "main")
            _ = ledger.ticket_base(ticket)
        return ticket, tuple(ids)

    def write_approved_phase(self) -> None:
        """Write the phase of the queued approved ticket at a synced tip, where the ledger finds the cells that record takes."""
        self.sync()
        ledger = self.ledger()
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())

    def reference_gate(self, ticket: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = style_plan.gate(self.repo, ticket)
        return result, output.getvalue()

    def fake_fmt(self, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        for name, text in (("stdout", stdout), ("stderr", stderr), ("exit", str(code))):
            _ = (self.root / "fmt/rustfmt" / name).write_text(text)

    def unformatted(self, *paths: str) -> None:
        self.fake_fmt("".join("{cwd}/" + path + "\n" for path in paths), code=1)

    def rustfmt_calls(self) -> list[str]:
        return fake_fmt.calls(self.root / "fmt", "rustfmt")

    LEDGER_MOD: str = ("mod handle;\nmod lock;\nmod map;\n\npub use lock::{LockError, MutationLock};\n\n"
                  + "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n"
                  + "    fn reads() {\n        let _ = lock::MutationLock;\n    }\n}\n")

    LEDGER_HANDLE: str = ("// Takes the lock.\nuse super::lock::MutationLock;\nuse crate::ledger::lock;\n\n"
                     + "pub struct Handle {\n    lock: MutationLock,\n}\n\n"
                     + "pub fn take(handle: &Handle) -> lock::LockError {\n    let _ = &handle.lock;\n"
                     + "    self::lock();\n    lock::LockError\n}\n\nfn lock() {}\n")

    def file_rename_ticket(self, files: tuple[tuple[str, str], ...] = (),
                           pairs: tuple[tuple[str, str], ...] = ()) -> str:
        """An approved ticket that renames crates/alpha/src/ledger/lock.rs to mutation_lock.rs, at a HEAD that holds it.

        Files replace or add HEAD files, and each pair adds a finding that renames an item of the file.
        """
        old = "crates/alpha/src/ledger/lock.rs"
        self.write(self.sources[0], "pub mod ledger;\n")
        self.write("crates/alpha/src/ledger/mod.rs", self.LEDGER_MOD)
        self.write("crates/alpha/src/ledger/handle.rs", self.LEDGER_HANDLE)
        self.write("crates/alpha/src/ledger/map.rs", "pub fn lock() {}\n")
        self.write(old, "pub struct MutationLock;\npub struct LockError;\n")
        for path, text in files:
            self.write(path, text)
        _ = self.commit()
        rule = self.rule("file-rename")
        issue = self.root / "file-rename-approval.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            findings = [ledger.hold(rule, old, 1, "pub struct MutationLock;", "rename", "The file contains MutationLock.",
                                    "Rename ledger/lock.rs to ledger/mutation_lock.rs; update ledger/mod.rs and all "
                                    + "super::lock paths", blob, "outside")]
            findings.extend(ledger.hold(rule, old, 2, "pub struct LockError;", "rename", before, after, blob, "outside")
                            for before, after in pairs)
            for finding in findings:
                ledger.file_held(finding.finding_id, issue)
            _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                                 + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [old], [rule],
                              proposal=str(issue), finding_ids=tuple(finding.finding_id for finding in findings))
        _ = self.git("mv", old, "crates/alpha/src/ledger/mutation_lock.rs")
        return "names-unit-1"

    def renamed_ledger(self, handle: str = LEDGER_HANDLE, error: str = "LockError") -> None:
        """The edits a file rename makes outside the file, ordered as rustfmt orders them, with LockError named error."""
        self.write("crates/alpha/src/ledger/mod.rs", self.LEDGER_MOD.replace(
            "mod lock;\nmod map;\n\npub use lock::{LockError, MutationLock};",
            f"mod map;\nmod mutation_lock;\n\npub use mutation_lock::{{{error}, MutationLock}};").replace(
            "lock::MutationLock;\n    }", "mutation_lock::MutationLock;\n    }"))
        self.write("crates/alpha/src/ledger/handle.rs", handle.replace(
            "use super::lock::MutationLock;\nuse crate::ledger::lock;",
            "use crate::ledger::mutation_lock;\nuse super::mutation_lock::MutationLock;").replace(
            "lock::LockError", f"mutation_lock::{error}"))

    MODULE_DOCS: str = "/// Alpha API.\n#[enabled(alpha)]\npub mod alpha;\n#[enabled({1,})]\npub mod beta;\n"

    MODULE_EDITS: tuple[tuple[str, str], ...] = (("/// Alpha API.\n#[enabled(alpha)]\npub mod alpha;\n", "#[enabled(alpha)]\npub mod alpha;\n/// Alpha API.\n"),
                    ("/// Alpha API.\n#[enabled(alpha)]", "#[enabled(\n    /// Alpha API.\n    alpha\n)]"),
                    ("#[enabled({1,})]", "#[enabled({1})]"))

    MACRO_COMMAS: str = ("macro_rules! enabled {\n    ([1,]) => { true };\n    ([1]) => { false };\n}\n\n"
                    + "pub fn on() -> bool {\n    enabled!([1,])\n}\n\n#[enabled([1,])]\npub fn off() {}\n")

    UNIT_TESTS: str = ('#[path = "../src/ledger/handle.rs"]\nmod handle;\n#[path = "../src/ledger/lock.rs"]\nmod lock;\n'
                  + '#[path = "../src/ledger/map.rs"]\nmod map;\n')

    LOCK: str = ("//! Holds the mutation lock.\nuse std::fmt;\nuse crate::ledger::handle::Handle;\n\n"
            + "/// The lock a mutation takes.\npub struct MutationLock;\npub struct LockError;\n\n"
            + "pub fn owner(_: &Handle) -> crate::ledger::lock::MutationLock {\n    MutationLock\n}\n")

    RENAMED_REFUSAL: str = ("crates/alpha/src/ledger/mutation_lock.rs (renamed from crates/alpha/src/ledger/lock.rs: "
                       + "change is not only the rename)")

    def renamed_declaration_gate(self, pairs: tuple[tuple[str, str], ...]) -> tuple[int, str]:
        """The gate after a file rename whose new file also renames its LockError declaration to LockFailure."""
        new = "crates/alpha/src/ledger/mutation_lock.rs"
        ticket = self.file_rename_ticket(pairs=pairs)
        self.renamed_ledger(error="LockFailure" if pairs else "LockError")
        self.write(new, (self.repo / new).read_text().replace("LockError", "LockFailure"))
        return self.reference_gate(ticket)

    def fake_cargo(self, stdout: str, code: int = 0) -> None:
        for name, text in (("stdout", stdout), ("exit", str(code))):
            _ = (self.root / "fmt/cargo" / name).write_text(text)

    CARGO_NOTES: str = ('[lib (2021)] "{cwd}/crates/alpha/src/lib.rs"\n'
                   + "rustfmt --edition 2021 --verbose --check -l {cwd}/crates/alpha/src/lib.rs\n"
                   + "Using rustfmt config file {cwd}/rustfmt.toml for {cwd}/crates/alpha/src/lib.rs\n")

    def take_style_work_into_judged_ticket(self) -> style_ledger.TicketRecord:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open("names-unit")[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        self.write("incoming.md", "Documentation from the branch point.\n")
        incoming = self.commit()
        _ = self.git("merge", "--ff-only", "main", cwd=self.style)
        _ = self.git("merge", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), incoming)
        return ticket

    def unit_commit(self, path: str, text: str) -> str:
        """Commit path on the style branch, and merge it into the names unit, so the unit base holds a commit that the
        synced tip lacks while no unit holds the file."""
        _ = (self.style / path).write_text(text)
        _ = self.git("commit", "-qam", "Style change", cwd=self.style)
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def only_first_source_open(self) -> str:
        """A read rule synced at the tip, with every source but the first judged clean."""
        read = self.rule("read")
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        return read

    def waiting_ticket(self) -> tuple[style_ledger.Ledger, str, str]:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        ledger.ticket_assign("names-unit-1", "demo-style-names")
        _ = ledger.ticket_base("names-unit-1")
        _ = (self.unit / self.sources[0]).write_text("pub fn checkpoint_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Waiting ticket", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(rule, self.sources[0], checkpoint, "names-unit-1")
        ledger.ticket_checkpoint("names-unit-1", checkpoint)
        return ledger, rule, checkpoint


    def widen_for_pipeline(self, ticket: str, path: str) -> None:
        with redirect_stdout(io.StringIO()):
            style_plan.widen(self.repo, ticket, (path,), "reference needed", "showrunner")

