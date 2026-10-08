#!/usr/bin/env python3
"""Add one unit to a running production, with restartable launch steps."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo

import unit_lookup


class Production(NamedTuple):
    doc: Path
    slug: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo


class PlanGiven(NamedTuple):
    path: Path


class BriefGiven(NamedTuple):
    words: str
    stub: Path


class Standby(NamedTuple):
    pass


class NewSession(NamedTuple):
    pass


class ResumedSession(NamedTuple):
    session_id: str
    cwd: Path


class DefaultEffort(NamedTuple):
    pass


class Effort(NamedTuple):
    value: str


class DirectorAgent(NamedTuple):
    model: str
    effort: DefaultEffort | Effort


class OmittedCell(NamedTuple):
    pass


class SuppliedCell(NamedTuple):
    value: str


class NoUnitRow(NamedTuple):
    pass


class ExistingUnitRow(NamedTuple):
    plan: str
    worktree: str
    branch: str
    port: str
    owns: str


class UnitIdentity(NamedTuple):
    unit: str
    # The name the session is launched under. It is not recorded: the user may rename the session.
    session: str


@dataclass(frozen=True, kw_only=True)
class UnitLaunchConfiguration:
    production: Production
    branch: str
    worktree: Path
    plan: PlanGiven | BriefGiven | Standby
    port: OmittedCell | SuppliedCell
    owns: OmittedCell | SuppliedCell
    session: NewSession | ResumedSession
    timeout: float

    @property
    def plan_path(self) -> Path:
        if isinstance(self.plan, PlanGiven):
            return self.plan.path
        if isinstance(self.plan, BriefGiven):
            return self.plan.stub
        raise ValueError("standby has no plan path")

    @property
    def plan_cell(self) -> str:
        return "standby" if isinstance(self.plan, Standby) else self.plan_path.as_posix()

    @property
    def mode_name(self) -> str:
        if isinstance(self.plan, PlanGiven):
            return "plan"
        return "brief" if isinstance(self.plan, BriefGiven) else "standby"


@dataclass(frozen=True, kw_only=True)
class RequestedUnitLaunch(UnitLaunchConfiguration):
    requested_name: str
    unit: str


@dataclass(frozen=True, kw_only=True)
class UnitLaunch(UnitLaunchConfiguration):
    identity: UnitIdentity


class ReadyToLaunch(NamedTuple):
    launch: UnitLaunch
    row: NoUnitRow | ExistingUnitRow
    director: DirectorAgent


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


def cell_value(value: str) -> str:
    """Read the value before a cell's commentary."""
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without interpreting its commentary."""
    prefix = f"- **{field}:** "
    for line in lines:
        if line.startswith(prefix):
            result = cell_value(line[len(prefix):])
            if result:
                return result
    raise Refusal(f"production doc lacks {field}")


def read_production(path: Path) -> Production:
    """Read the production fields shared with merge_checkpoint.py."""
    try:
        doc = path.resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        zone = ZoneInfo(production_field(lines, "User zone"))
        slug = doc.name.removesuffix("-production.md")
        if slug == doc.name:
            raise Refusal("production doc name must end in -production.md")
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        if "## Units" not in lines:
            raise Refusal("production doc lacks Units table")
        return Production(doc, slug, merge_branch, checkout, checkout / log, zone)
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def git(production: Production, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(production.checkout), *args], text=True,
                          capture_output=True, check=check)


def relative_plan(production: Production, raw: str) -> Path:
    path = Path(raw).expanduser()
    resolved = (path if path.is_absolute() else production.checkout / path).resolve()
    try:
        return resolved.relative_to(production.checkout)
    except ValueError as error:
        raise Refusal("plan must be inside Showrunner checkout") from error


def stub_plan(production: Production, branch: str) -> Path:
    """Name a brief's stub plan beside the production doc, relative to Showrunner checkout."""
    try:
        return production.doc.parent.relative_to(production.checkout) / f"{branch}.md"
    except ValueError as error:
        raise Refusal("production doc must be inside Showrunner checkout") from error


def launch_request(args: argparse.Namespace) -> RequestedUnitLaunch:
    production = read_production(Path(cast(str, args.production)))
    name = cast(str, args.name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise Refusal("name must contain only letters, numbers, hyphens, or underscores")
    given_plan = cast(str | None, args.plan)
    given_brief = cast(str | None, args.brief)
    standby = cast(bool, args.standby)
    if sum((given_plan is not None, given_brief is not None, standby)) != 1:
        raise Refusal("give exactly one of --plan, --brief, or --standby")
    kebab = name.replace("_", "-")
    # The slug names the branch, as it names the production's plans; the merge branch may differ.
    branch = f"{production.slug}-{kebab}"
    checkout_name = production.checkout.name.removesuffix("-trunk")
    worktree = production.checkout.parent / f"{checkout_name}-{kebab}"
    plan: PlanGiven | BriefGiven | Standby
    if given_plan is not None:
        plan = PlanGiven(relative_plan(production, given_plan))
    elif given_brief is not None:
        assert given_brief is not None
        plan = BriefGiven(given_brief, stub_plan(production, branch))
    else:
        plan = Standby()
    resumed = cast(str | None, args.resume)
    cwd = cast(str | None, args.cwd)
    if (resumed is None) != (cwd is None):
        raise Refusal("--resume and --cwd must be given together")
    if resumed is not None and not isinstance(plan, PlanGiven):
        raise Refusal("--resume requires --plan")
    session: NewSession | ResumedSession = (ResumedSession(resumed, Path(cwd).expanduser().resolve())
                                            if resumed is not None and cwd is not None else NewSession())
    port = cast(int | None, args.port)
    timeout = cast(float, args.timeout)
    if port is not None and not 1 <= port <= 65535:
        raise Refusal("--port must be between 1 and 65535")
    if timeout < 0:
        raise Refusal("--timeout must be nonnegative")
    owns = cast(str | None, args.owns)
    return RequestedUnitLaunch(
        production=production,
        requested_name=name,
        unit=f"{name}-unit",
        branch=branch,
        worktree=worktree,
        plan=plan,
        port=SuppliedCell(str(port)) if port is not None else OmittedCell(),
        owns=SuppliedCell(owns) if owns is not None else OmittedCell(),
        session=session,
        timeout=timeout,
    )


def unit_rows(lines: list[str]) -> tuple[int, list[str]]:
    try:
        start = lines.index("## Units")
    except ValueError as error:
        raise Refusal("production doc lacks Units table") from error
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    rows = [line for line in lines[start + 1:end] if line.startswith("|")]
    if len(rows) < 2 or not rows[0].startswith("| Unit |"):
        raise Refusal("production doc has invalid Units table")
    insert_at = max(i for i in range(start + 1, end) if lines[i].startswith("|")) + 1
    return insert_at, rows[2:]


def row_cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def unit_headings(lines: list[str]) -> list[str]:
    """The Units table's column headings, in the doc's own order."""
    _ = unit_rows(lines)
    return row_cells(next(line for line in lines[lines.index("## Units"):] if line.startswith("| Unit |")))


def unit_table(lines: list[str]) -> list[dict[str, str]]:
    """Each Units row as its cells by column heading, as written.

    Reading by heading lets a doc that still has the retired Session column read like one without it.
    """
    headings = unit_headings(lines)
    _, rows = unit_rows(lines)
    return [dict(zip(headings, row_cells(row), strict=False)) for row in rows]


def plan_cell_is_retired(plan_cell: str) -> bool:
    """Return whether a Plan cell begins with the retired marker."""
    return re.match(r"^\s*\(?retired\b", plan_cell) is not None


def worktree_is_linked(worktree: str) -> bool:
    """Return whether `worktree` is a linked worktree on disk.

    A main checkout, where `.git` is a directory, is shared and is no unit's own worktree.
    """
    return bool(worktree) and (Path(worktree) / ".git").is_file()


class SessionMarks:
    """Which of a production's units have a tmux session, asked of tmux once and only when a row needs it."""

    def __init__(self, slug: str) -> None:
        self._slug: str = slug
        self._units: set[str] | None = None
        self._unknown: bool = False

    def has(self, unit: str) -> bool:
        """Whether a tmux session carries the unit's mark. A tmux that cannot say has not said no."""
        if self._units is None:
            try:
                self._units = set(unit_lookup.marked_units(self._slug))
            except OSError:
                self._units, self._unknown = set(), True
        return self._unknown or unit in self._units


def unit_is_retired(cells: dict[str, str], marks: SessionMarks) -> bool:
    """Return whether a Units row's unit is retired.

    It is when its Plan cell says so, or when its worktree and its tmux session are both gone
    (user 2026-10-08): such a unit has nothing left to address. The worktree is asked first, so a
    unit whose session is only restarting keeps its row, and tmux is asked only about a unit
    with no worktree of its own. The session is the one carrying the unit's mark, whatever its name.
    """
    if plan_cell_is_retired(cells.get("Plan", "")):
        return True
    if "Worktree" not in cells:
        return False
    return (not worktree_is_linked(cell_value(cells["Worktree"]))
            and not marks.has(cell_value(cells.get("Unit", ""))))


def live_unit_table(lines: list[str], slug: str) -> list[dict[str, str]]:
    """Return Units rows, by column heading, whose unit is not retired."""
    marks = SessionMarks(slug)
    return [cells for cells in unit_table(lines) if not unit_is_retired(cells, marks)]


def retired_units(lines: list[str], slug: str) -> set[str]:
    """Return unit names from Units rows whose unit is retired."""
    marks = SessionMarks(slug)
    retired = {cell_value(cells.get("Unit", "")) for cells in unit_table(lines) if unit_is_retired(cells, marks)}
    return retired - {""}


def desired_row(request: UnitLaunch, headings: list[str]) -> str:
    """The unit's row in the doc's own columns. A column this script does not fill gets a dash."""
    cells = {"Unit": request.identity.unit, "Plan": request.plan_cell, "Worktree": str(request.worktree),
             "Branch": request.branch}
    for heading, cell in (("Port", request.port), ("Owns", request.owns)):
        if isinstance(cell, SuppliedCell):
            cells[heading] = cell.value
    return "| " + " | ".join(cells.get(heading, "—") for heading in headings) + " |"


def row_is_present(request: RequestedUnitLaunch, lines: list[str]) -> NoUnitRow | ExistingUnitRow:
    matching: list[ExistingUnitRow] = []
    for cells in unit_table(lines):
        fields = {heading: cell_value(cell) for heading, cell in cells.items()}
        if fields.get("Unit") == request.unit:
            try:
                matching.append(ExistingUnitRow(fields["Plan"], fields["Worktree"], fields["Branch"],
                                                fields["Port"], fields["Owns"]))
            except KeyError as error:
                raise Refusal(f"unit {request.unit} has an invalid Units row") from error
        elif fields.get("Worktree") == str(request.worktree) or fields.get("Branch") == request.branch:
            raise Refusal(f"branch or worktree for {request.unit} is already used in Units table")
    if len(matching) > 1:
        raise Refusal(f"unit name {request.unit} is already taken in Units table")
    if not matching:
        return NoUnitRow()
    existing = matching[0]
    for cell, actual, expected in (("Plan", existing.plan, request.plan_cell),
                                   ("Worktree", existing.worktree, str(request.worktree)),
                                   ("Branch", existing.branch, request.branch)):
        if actual != expected:
            raise Refusal(f"unit {request.unit} {cell} is {actual!r}; expected {expected!r}")
    for cell, actual, supplied in (("Port", existing.port, request.port),
                                   ("Owns", existing.owns, request.owns)):
        if isinstance(supplied, SuppliedCell) and actual != supplied.value:
            raise Refusal(f"unit {request.unit} {cell} is {actual!r}; expected {supplied.value!r}")
    return existing


def recorded_mode(request: UnitLaunch, row: ExistingUnitRow) -> str:
    subject_prefix = f"production({request.production.slug}): add unit {request.identity.unit} ("
    subjects = git(request.production, "log", "--format=%s", "--", str(
        request.production.doc.relative_to(request.production.checkout))).stdout.splitlines()
    for subject in subjects:
        if subject in (subject_prefix + "plan)", subject_prefix + "brief)", subject_prefix + "standby)"):
            return subject.removeprefix(subject_prefix).removesuffix(")")
    if row.plan == "standby":
        return "standby"
    stub = request.production.checkout / stub_plan(request.production, request.branch)
    if stub.exists() and request.plan_path == stub.relative_to(request.production.checkout):
        if "## Source\n\n" in stub.read_text(encoding="utf-8"):
            return "brief"
    return "plan"


def worktree_on_branch(request: UnitLaunch) -> bool:
    worktree = request.worktree
    if not worktree.exists() and not worktree.is_symlink():
        return False
    top = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
                         text=True, capture_output=True, check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree.resolve():
        raise Refusal(f"worktree path {worktree} is already in use")
    checkout_git = git(request.production, "rev-parse", "--git-common-dir").stdout.strip()
    worktree_git = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
                                  text=True, capture_output=True, check=False)
    checkout_common = (request.production.checkout / checkout_git).resolve()
    worktree_common = (worktree / worktree_git.stdout.strip()).resolve()
    if worktree_git.returncode != 0 or worktree_common != checkout_common:
        raise Refusal(f"worktree path {worktree} is already in use")
    branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                            text=True, capture_output=True, check=False)
    if branch.returncode != 0 or branch.stdout.strip() != request.branch:
        raise Refusal(f"worktree {worktree} is not on {request.branch}")
    return True


def tmux_live(tmux: str, name: str) -> bool:
    result = subprocess.run([tmux, "has-session", "-t", f"={name}"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def director_agent(request: UnitLaunch) -> DirectorAgent:
    """Resolve the unit director in this script's checkout before any writes."""
    resolver = Path(__file__).resolve().parents[1] / "agents" / "agents_config.sh"
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && agents_resolve production.director && printf "%s\\n%s\\n%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"',
         "_", str(resolver)], cwd=request.production.checkout,
        text=True, capture_output=True, check=False)
    if result.returncode != 0:
        error = next((line for line in result.stderr.splitlines() if line.startswith("ERROR:")),
                     result.stderr.splitlines()[0] if result.stderr else "ERROR: cannot resolve production.director.")
        raise Refusal(error)
    values = result.stdout.splitlines()
    if len(values) != 3 or not values[0] or not values[1]:
        raise Refusal("ERROR: production.director resolver returned invalid output.")
    family, model, effort = values
    if family != "claude":
        raise Refusal(f"unit directors launch only on claude; production.director resolves to {family} ({model})")
    return DirectorAgent(model, Effort(effort) if effort else DefaultEffort())


def preflight(request: RequestedUnitLaunch) -> ReadyToLaunch:
    production = request.production
    branch = git(production, "branch", "--show-current").stdout.strip()
    if branch != production.merge_branch:
        raise Refusal(f"Showrunner checkout is not on {production.merge_branch}")
    try:
        _ = production.doc.relative_to(production.checkout)
    except ValueError as error:
        raise Refusal("production doc must be inside Showrunner checkout") from error
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    existing = row_is_present(request, lines)
    launch = UnitLaunch(
        production=request.production,
        identity=UnitIdentity(request.unit, request.requested_name),
        branch=request.branch,
        worktree=request.worktree,
        plan=request.plan,
        port=request.port,
        owns=request.owns,
        session=request.session,
        timeout=request.timeout,
    )
    if isinstance(existing, ExistingUnitRow) and recorded_mode(launch, existing) != launch.mode_name:
        raise Refusal(f"unit name {launch.identity.unit} is already taken in Units table")
    has_worktree = worktree_on_branch(launch)
    if has_worktree and isinstance(existing, NoUnitRow):
        raise Refusal(f"worktree path {request.worktree} is already in use")
    local_branch = git(production, "show-ref", "--verify", "--quiet",
                       f"refs/heads/{request.branch}", check=False).returncode == 0
    if (local_branch or remote_head(production, request.branch)) and not (
            isinstance(existing, ExistingUnitRow) and has_worktree):
        raise Refusal(f"branch {request.branch} already exists")
    if isinstance(launch.plan, PlanGiven):
        result = git(production, "show", f"{production.merge_branch}:{launch.plan.path.as_posix()}", check=False)
        if result.returncode != 0 or not any(line.startswith("> **Production:") for line in result.stdout.splitlines()):
            raise Refusal(f"plan {launch.plan.path} is missing on {production.merge_branch} or lacks the Production header")
    elif isinstance(launch.plan, BriefGiven) and (production.checkout / launch.plan.stub).exists():
        stub = production.checkout / launch.plan.stub
        content = stub.read_text(encoding="utf-8")
        source = content.split("## Source\n\n", 1)
        saved_words = source[1].split("\n\n", 1) if len(source) == 2 else []
        if (not any(line.startswith("> **Production:") for line in content.splitlines())
                or len(saved_words) != 2 or saved_words[1] != launch.plan.words + "\n"):
            raise Refusal(f"stub plan {launch.plan.stub} already exists with different content")
    return ReadyToLaunch(launch, existing, director_agent(launch))


def write_stub(request: UnitLaunch) -> None:
    if not isinstance(request.plan, BriefGiven):
        return
    path = request.production.checkout / request.plan.stub
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = request.production.doc.relative_to(request.production.checkout).as_posix()
    stamp = datetime.now(request.production.zone).strftime("%Y-%m-%d %H:%M %Z")
    header = f"# {request.identity.session}\n\n> **Production: {request.production.slug}** — unit "
    source = f"`{request.identity.unit}`; production doc `{doc}`\n\n## Source\n\n{stamp}\n\n{request.plan.words}\n"
    _ = path.write_text(header + source, encoding="utf-8")


def append_row(request: UnitLaunch, existing: NoUnitRow | ExistingUnitRow) -> None:
    if isinstance(existing, ExistingUnitRow):
        return
    doc = request.production.doc
    lines = doc.read_text(encoding="utf-8").splitlines()
    insert_at, _ = unit_rows(lines)
    lines.insert(insert_at, desired_row(request, unit_headings(lines)))
    _ = doc.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remote_head(production: Production, branch: str) -> str:
    result = git(production, "ls-remote", "--heads", "origin", branch)
    return result.stdout.split()[0] if result.stdout.strip() else ""


def commit_paths(production: Production, paths: list[Path], message: str) -> None:
    """Commit `paths` on the merge branch and push nothing of it.

    The commit goes out with the showrunner's next merge push: a push here would carry every
    unpushed merge with it and start CI where pushes to the merge branch do (user, 2026-10-08).
    """
    relative_paths = [str(path.relative_to(production.checkout) if path.is_absolute() else path)
                      for path in paths]
    status = git(production, "status", "--porcelain", "--", *relative_paths).stdout
    if status:
        _ = git(production, "add", "--", *relative_paths)
        _ = git(production, "commit", "--only", "-m", message, "--", *relative_paths)


def commit_unit(request: UnitLaunch) -> None:
    paths = [request.production.doc]
    if isinstance(request.plan, BriefGiven):
        paths.append(request.production.checkout / request.plan.stub)
    commit_paths(
        request.production,
        paths,
        f"production({request.production.slug}): add unit {request.identity.unit} ({request.mode_name})",
    )


def ensure_worktree(request: UnitLaunch) -> None:
    """Create the unit's worktree on its own branch and push nothing.

    The unit director's first checkpoint push puts the branch on origin and sets its upstream
    (user, 2026-10-08).
    """
    production = request.production
    worktree = request.worktree
    if worktree.exists():
        _ = worktree_on_branch(request)
    else:
        _ = git(production, "worktree", "add", str(worktree), "-b", request.branch,
                production.merge_branch)
    if (production.checkout / ".claude/config/berth.toml").exists():
        key = f"branch.{request.branch}.cargoBerthTarget"
        current = git(production, "config", "--get", key, check=False).stdout.strip()
        if current != production.merge_branch:
            _ = git(production, "config", key, production.merge_branch)


def prompt_for(request: UnitLaunch) -> str:
    production = request.production
    doc = str(production.doc)
    # The showrunner's name is written nowhere: a unit looks it up, since a rename changes it.
    showrunner = ("the showrunner, whose session name `~/.claude/scripts/lib/py "
                  f"~/.claude/scripts/production/showrunners.py name {production.slug}` prints (look it up "
                  "before each message: the name can change)")
    if isinstance(request.plan, Standby):
        return (f"You are {request.identity.unit} in production {production.slug} (doc {doc}), under "
                f"{showrunner}, on standby. Work only in your worktree "
                f"{request.worktree}, branch {request.branch}. Do nothing until the showrunner sends you work.")
    plan = request.plan_path.as_posix()
    if isinstance(request.session, ResumedSession):
        plan = str(request.worktree / request.plan_path)
        return (f"You are now {request.identity.unit} in production {production.slug} (doc {doc}), under "
                f"{showrunner}. Work only in your worktree {request.worktree}, "
                f"branch {request.branch}, and name it in every Work Order. Run /unit:delegate {plan}.")
    if isinstance(request.plan, PlanGiven):
        return f"/unit:delegate {plan}"
    return (f"You are {request.identity.unit} in production {production.slug} (doc {doc}), under "
            f"{showrunner}. Work only in your worktree {request.worktree}, "
            f"branch {request.branch}, and name it in every Work Order. Your plan {plan} holds only the "
            f"user's words. Write the full phased plan there, send it to the showrunner, and wait for its "
            f"approval before you run /unit:delegate {plan}.")


def launch_session(request: UnitLaunch, tmux: str, director: DirectorAgent) -> None:
    """Start the unit's session in a tmux session marked as this unit of this production.

    The marks are what finds the unit afterwards; its name is only what it is called at launch.
    """
    marks = {unit_lookup.PRODUCTION_MARK: request.production.slug, unit_lookup.UNIT_MARK: request.identity.unit}
    argv = ["claude", "--model", director.model]
    if isinstance(director.effort, Effort):
        argv.extend(["--effort", director.effort.value])
    if isinstance(request.session, ResumedSession):
        argv.extend(["--resume", request.session.session_id])
    argv.extend(["--remote-control", request.identity.session, "-n", request.identity.session,
                 "--settings", '{"disableAgentView": true}', prompt_for(request)])
    command = "ENABLE_TOOL_SEARCH=true command " + shlex.join(argv) + "; exec zsh"
    cwd = request.session.cwd if isinstance(request.session, ResumedSession) else request.worktree
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}
    _ = subprocess.run(["systemd-run", "--user", "--scope", f"--unit={request.identity.session}", tmux,
                        "new-session", "-d", "-s", request.identity.session, "-c", str(cwd),
                        *(argument for name, value in marks.items() for argument in ("-e", f"{name}={value}")),
                        "zsh", "-ic", command],
                       env=environment, text=True, capture_output=True, check=True)


def launched_unit(request: UnitLaunch) -> unit_lookup.MarkedUnit | None:
    """The unit's tmux session, found by its mark."""
    return unit_lookup.marked_units(request.production.slug).get(request.identity.unit)


def wait_for_remote_control(request: UnitLaunch, tmux: str) -> unit_lookup.MarkedUnit:
    launched = launched_unit(request)
    if launched is None:
        raise RuntimeError(f"{request.identity.unit}: no tmux session carries its mark")
    deadline = time.monotonic() + request.timeout
    pane = ""
    while True:
        result = subprocess.run([tmux, "capture-pane", "-p", "-t", launched.pane],
                                text=True, capture_output=True, check=False)
        pane = result.stdout if result.returncode == 0 else result.stderr
        if "/remote-control is active" in pane:
            return launched
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = "\n".join(pane.splitlines()[-15:])
            raise RuntimeError(f"{launched.label}: /remote-control did not become active\n{tail}")
        time.sleep(min(0.5, remaining))


def record(request: UnitLaunch) -> None:
    production = request.production
    log_line = f"added {request.identity.unit} ({request.mode_name}), worktree {request.worktree}"
    if production.log.exists() and any(log_line in line for line in production.log.read_text(encoding="utf-8").splitlines()):
        return
    stamp = datetime.now(production.zone).strftime("%H:%M %Z")
    with production.log.open("a", encoding="utf-8") as log:
        _ = log.write(f"- {stamp}: {log_line}\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("name")
    _ = parser.add_argument("--plan")
    _ = parser.add_argument("--brief")
    _ = parser.add_argument("--standby", action="store_true")
    _ = parser.add_argument("--port", type=int)
    _ = parser.add_argument("--owns")
    _ = parser.add_argument("--timeout", type=float, default=90.0)
    _ = parser.add_argument("--resume")
    _ = parser.add_argument("--cwd")
    _ = parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = launch_request(args)
        ready = preflight(request)
        request = ready.launch
        if cast(bool, args.check):
            return 0
        tmux = unit_lookup.tmux_binary()
        already_launched = launched_unit(request) is not None
        if not already_launched and tmux_live(tmux, request.identity.session):
            raise Refusal(f"tmux session {request.identity.session} is already live and is not marked as "
                          + request.identity.unit)
        write_stub(request)
        append_row(request, ready.row)
        commit_unit(request)
        ensure_worktree(request)
        if not already_launched:
            launch_session(request, tmux, ready.director)
        launched = wait_for_remote_control(request, tmux)
        record(request)
        print(f"{request.identity.unit} started: tmux attach -t {launched.label}")
        return 0
    except Refusal as error:
        print(f"add_unit: {error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, subprocess.CalledProcessError, RuntimeError, ValueError) as error:
        detail = str(error)
        print(f"add_unit: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
