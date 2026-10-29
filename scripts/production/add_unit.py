#!/usr/bin/env python3
"""Add one unit to a running production, with restartable launch steps."""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast, override
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "hooks"))
sys.path.insert(0, str(SCRIPTS / "message"))
sys.path.insert(0, str(SCRIPTS / "shutdown"))

import autocompact
import rename_state
import showrunners
import sessions
import unit_lookup
import unit_names
from production_units import Production, Refusal, cell_value, plan_cell_is_retired, read_production, unit_rows, unit_headings, unit_table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from probe_session import PROBE_ENV  # noqa: E402

if TYPE_CHECKING:
    import herdr
    import launch_permission
    from scripts.accounts.accounts import ClaudeLaunchAccount
    from record import SessionShutdown


class ProductionPlanRequired(NamedTuple):
    """The launch needs a plan with a Production header."""


class ExistingConversationPlan(NamedTuple):
    """An existing director resumes with its own recorded plan, even after condensation."""


class PlanGiven(NamedTuple):
    path: Path


class BriefGiven(NamedTuple):
    words: str
    stub: Path


class Standby(NamedTuple):
    # A permanent unit is a standby unit that wrap never retires.
    permanent: bool = False


class StandardLaunch(NamedTuple):
    pass


class ProfiledLaunch(NamedTuple):
    """A unit that runs outside its worktree, with flags of its own and one more line of duty."""

    cwd: Path
    flags: tuple[str, ...]
    duty: str


LaunchProfile = StandardLaunch | ProfiledLaunch
NIGHTLY_REVIEW_UNIT = "nightly-review-unit"


def launch_profile(unit: str) -> LaunchProfile:
    """The nightly review reads every repository from ~/rust under its own read-only settings."""
    if unit != NIGHTLY_REVIEW_UNIT:
        return StandardLaunch()
    home = Path.home()
    return ProfiledLaunch(
        home / "rust",
        ("--add-dir", "/etc/nixos", "--add-dir", str(home / ".claude"),
         "--settings", str(SCRIPTS / "nightly_review" / "settings.json")),
        "You run read-only from ~/rust; your worktree holds no work. Each night the nightly timer sends you "
        + "the night's review: follow ~/.claude/commands/nightly_review.md for the night and modes it names.",
    )


class NewSession(NamedTuple):
    pass


class ResumeStoppedSession(NamedTuple):
    """The caller expects no process to be running on the conversation."""


class AttachRunningSession(NamedTuple):
    """The caller requests a safe shutdown before the conversation becomes a unit."""


class ResumedSession(NamedTuple):
    session_id: str
    cwd: Path
    handoff: ResumeStoppedSession | AttachRunningSession = ResumeStoppedSession()


class NoSessionHandoff(NamedTuple):
    pass


class RunningSessionHandoff(NamedTuple):
    session_id: str
    pid: int
    proc_start: str
    login: str


class StoppedSessionHandoff(NamedTuple):
    """The shutdown record confirms a stop and both old identity and registry are clear."""

    source: RunningSessionHandoff
    record: SessionShutdown

    @property
    def session_id(self) -> str:
        return self.source.session_id


class NewWorkLaunch(NamedTuple):
    pass


class ResumeInStartFolderLaunch(NamedTuple):
    """A retired unit resumes outside its worktree and needs no worktree or branch."""


class UnitRestoreLaunch(NamedTuple):
    session_id: str
    note: Path
    session_name: str
    # The unit's herdr session it is restored into.
    host_session: str
    probe: bool = False


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


class ActiveUnitRow(NamedTuple):
    pass


class RetiredUnitRow(NamedTuple):
    pass


class ExistingUnitRow(NamedTuple):
    plan: str
    worktree: str
    branch: str
    port: str
    owns: str
    state: ActiveUnitRow | RetiredUnitRow


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
    launch_kind: NewWorkLaunch | UnitRestoreLaunch | ResumeInStartFolderLaunch
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
        if isinstance(self.plan, Standby):
            return "permanent" if self.plan.permanent else "standby"
        return self.plan_path.as_posix()

    @property
    def mode_name(self) -> str:
        if isinstance(self.plan, PlanGiven):
            return "plan"
        return "brief" if isinstance(self.plan, BriefGiven) else self.plan_cell

    @property
    def profile(self) -> LaunchProfile:
        return launch_profile(self.unit_id)

    @property
    def unit_id(self) -> str:
        raise NotImplementedError

    @property
    def session_cwd(self) -> Path:
        """Where the session runs: a resumed one where its transcript lives, a new one in the worktree
        or where its launch profile names."""
        if isinstance(self.session, ResumedSession):
            return self.session.cwd
        profile = self.profile
        return profile.cwd if isinstance(profile, ProfiledLaunch) else self.worktree


@dataclass(frozen=True, kw_only=True)
class RequestedUnitLaunch(UnitLaunchConfiguration):
    requested_name: str
    unit: str

    @property
    @override
    def unit_id(self) -> str:
        return self.unit


@dataclass(frozen=True, kw_only=True)
class UnitLaunch(UnitLaunchConfiguration):
    identity: UnitIdentity

    @property
    @override
    def unit_id(self) -> str:
        return self.identity.unit


class ReadyToLaunch(NamedTuple):
    launch: UnitLaunch
    row: NoUnitRow | ExistingUnitRow
    director: DirectorAgent
    handoff: NoSessionHandoff | RunningSessionHandoff = NoSessionHandoff()


@dataclass(frozen=True)
class MarkedUnitFound:
    """The unit already has a session carrying its marks."""

    unit: unit_lookup.MarkedUnit


@dataclass(frozen=True)
class NoMarkedUnit:
    """No session carries the requested unit's marks."""


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
    try:
        # A name given as its unit id would get the suffix twice: feature-log-unit-unit.
        name = unit_names.name(cast(str, args.name))
    except unit_names.BadName as error:
        raise Refusal(str(error)) from error
    given_plan = cast(str | None, args.plan)
    given_brief = cast(str | None, args.brief)
    standby = cast(bool, args.standby)
    permanent = cast(bool, args.permanent)
    if sum((given_plan is not None, given_brief is not None, standby, permanent)) != 1:
        raise Refusal("give exactly one of --plan, --brief, --standby, or --permanent")
    kebab = unit_names.stem(unit_names.unit_id(name))
    branch = unit_names.branch(production.slug, kebab)
    worktree = unit_names.worktree(production.checkout, kebab)
    plan: PlanGiven | BriefGiven | Standby
    if given_plan is not None:
        plan = PlanGiven(relative_plan(production, given_plan))
    elif given_brief is not None:
        assert given_brief is not None
        plan = BriefGiven(given_brief, stub_plan(production, branch))
    else:
        plan = Standby(permanent)
    resumed = cast(str | None, args.resume)
    attached = cast(str | None, getattr(args, "attach", None))
    identity = attached if attached is not None else resumed
    session_flag = "--attach" if attached is not None else "--resume"
    cwd = cast(str | None, args.cwd)
    restart_note = cast(str | None, args.restart_note)
    session_name = cast(str | None, args.session_name)
    host_session = cast(str | None, args.host_session)
    restart_values = (restart_note, session_name, host_session)
    if any(value is not None for value in restart_values) and not all(
        value is not None for value in restart_values
    ):
        raise Refusal(
            "--restart-note, --session-name and --host-session must be given together"
        )
    if restart_note is not None and resumed is None:
        raise Refusal("restart launch flags require --resume")
    probe = cast(bool, args.probe)
    if probe and restart_note is None:
        raise Refusal("--probe requires a shutdown restore with --restart-note")
    if (identity is None) != (cwd is None):
        raise Refusal(f"{session_flag} and --cwd must be given together")
    if identity is not None and isinstance(plan, BriefGiven):
        raise Refusal(f"{session_flag} requires --plan, --standby, or --permanent")
    session: NewSession | ResumedSession = (
        ResumedSession(identity, Path(cwd).expanduser().resolve(),
                       AttachRunningSession() if attached is not None else ResumeStoppedSession())
        if identity is not None and cwd is not None else NewSession()
    )
    launch_kind: NewWorkLaunch | UnitRestoreLaunch
    if (
        resumed is not None
        and restart_note is not None
        and session_name is not None
        and host_session is not None
    ):
        launch_kind = UnitRestoreLaunch(
            resumed,
            Path(restart_note).expanduser(),
            session_name,
            host_session,
            probe,
        )
    else:
        launch_kind = NewWorkLaunch()
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
        unit=(name if any(cell_value(row["Unit"]) == name
                          for row in unit_table(production.doc.read_text(encoding="utf-8").splitlines()))
              else f"{name}-unit"),
        branch=branch,
        worktree=worktree,
        plan=plan,
        port=SuppliedCell(str(port)) if port is not None else OmittedCell(),
        owns=SuppliedCell(owns) if owns is not None else OmittedCell(),
        session=session,
        launch_kind=launch_kind,
        timeout=timeout,
    )


def desired_row(request: UnitLaunch, headings: list[str]) -> str:
    """The unit's row in the doc's own columns. A column this script does not fill gets a dash."""
    cells = {"Unit": request.identity.unit, "Plan": request.plan_cell, "Worktree": str(request.worktree),
             "Branch": request.branch}
    for heading, cell in (("Port", request.port), ("Owns", request.owns)):
        if isinstance(cell, SuppliedCell):
            cells[heading] = cell.value
    return "| " + " | ".join(cells.get(heading, "—") for heading in headings) + " |"


def row_is_present(request: RequestedUnitLaunch, lines: list[str]) -> NoUnitRow | ExistingUnitRow:
    # A restore creates no worktree or branch, so the naming pattern behind `request.worktree` and
    # `request.branch` neither reserves them against other rows nor has to match the unit's own row.
    restoring = isinstance(request.launch_kind, UnitRestoreLaunch)
    # A resumed session keeps its row's worktree and branch, whatever the naming rule now gives.
    row_names_place = restoring or isinstance(request.session, ResumedSession)
    matching: list[ExistingUnitRow] = []
    for cells in unit_table(lines):
        fields = {heading: cell_value(cell) for heading, cell in cells.items()}
        if fields.get("Unit") == request.unit:
            try:
                state = RetiredUnitRow() if plan_cell_is_retired(cells["Plan"]) else ActiveUnitRow()
                if (isinstance(state, RetiredUnitRow) and not restoring
                        and isinstance(request.session, ResumedSession)
                        and isinstance(request.session.handoff, ResumeStoppedSession)):
                    fields["Plan"] = cell_value(re.sub(r"^\s*\(retired [^)]*\)\s*", "", cells["Plan"]))
                matching.append(ExistingUnitRow(fields["Plan"], fields["Worktree"], fields["Branch"],
                                                fields["Port"], fields["Owns"], state))
            except KeyError as error:
                raise Refusal(f"unit {request.unit} has an invalid Units row") from error
        elif not restoring and (fields.get("Worktree") == str(request.worktree)
                                or fields.get("Branch") == request.branch):
            raise Refusal(f"branch or worktree for {request.unit} is already used in Units table")
    if len(matching) > 1:
        raise Refusal(f"unit name {request.unit} is already taken in Units table")
    if not matching:
        return NoUnitRow()
    existing = matching[0]
    pattern_cells = () if row_names_place else (("Worktree", existing.worktree, str(request.worktree)),
                                          ("Branch", existing.branch, request.branch))
    for cell, actual, expected in (("Plan", existing.plan, request.plan_cell), *pattern_cells):
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
        if subject in (subject_prefix + "plan)", subject_prefix + "brief)", subject_prefix + "standby)",
                       subject_prefix + "permanent)"):
            return subject.removeprefix(subject_prefix).removesuffix(")")
    if row.plan in ("standby", "permanent"):
        return row.plan
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


def _is_excluded_standing_plan(production: Production, plan: Path) -> bool:
    source = production.checkout / plan
    if not source.is_file():
        return False
    lines = source.read_text(encoding="utf-8").splitlines()
    return (any(line.startswith("> **Production:") for line in lines)
            and any(line.startswith("> **Standing:**") for line in lines)
            and git(production, "check-ignore", "-q", "--", plan.as_posix(), check=False).returncode == 0)


def preflight(request: RequestedUnitLaunch, selected: ClaudeLaunchAccount) -> ReadyToLaunch:
    from scripts.accounts.accounts import trust_command
    from scripts.herdr.herdr import Untrusted, UnreadableTrustFile, claude_trust, repository_trust_key

    handoff: NoSessionHandoff | RunningSessionHandoff = NoSessionHandoff()
    if isinstance(request.session, ResumedSession):
        session_id = request.session.session_id
        observed = sessions.session_liveness(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise Refusal(f"cannot tell whether session {session_id} runs: {observed.reason}")
        if isinstance(request.session.handoff, AttachRunningSession):
            if isinstance(observed, sessions.SessionNotLive):
                raise Refusal(f"session {session_id} does not run; use --resume")
            handoff = attach_source(session_id, observed.pid)
        elif isinstance(observed, sessions.LiveSessionProcess):
            raise Refusal(f"session {session_id} already runs as pid {observed.pid}; "
                          + f"to make it this unit, run add_unit with --attach {session_id}")
    production = request.production
    branch = git(production, "branch", "--show-current").stdout.strip()
    if branch != production.merge_branch:
        raise Refusal(f"Showrunner checkout is not on {production.merge_branch}")
    if request.unit in rename_state.rename_claim(production.slug):
        raise Refusal(f"{request.unit} is being renamed; finish it with rename_unit.py apply")
    try:
        _ = production.doc.relative_to(production.checkout)
    except ValueError as error:
        raise Refusal("production doc must be inside Showrunner checkout") from error
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    existing = row_is_present(request, lines)
    restoring = isinstance(request.launch_kind, UnitRestoreLaunch)
    plan_context: ProductionPlanRequired | ExistingConversationPlan = ProductionPlanRequired()
    if (not restoring and isinstance(existing, ExistingUnitRow)
            and isinstance(request.session, ResumedSession)
            and isinstance(request.session.handoff, ResumeStoppedSession)):
        plan_context = ExistingConversationPlan()
    if restoring:
        if isinstance(existing, NoUnitRow):
            raise Refusal(f"unit {request.unit} has no Units row; a restore never adds one")
        # Its row, not the naming pattern, names a restored unit's worktree and branch.
        request = replace(request, worktree=Path(existing.worktree), branch=existing.branch)
    elif isinstance(request.session, ResumedSession) and isinstance(existing, ExistingUnitRow):
        if request.session.cwd != Path(existing.worktree).expanduser().resolve():
            if (isinstance(existing.state, RetiredUnitRow)
                    and isinstance(request.session.handoff, ResumeStoppedSession)):
                request = replace(request, launch_kind=ResumeInStartFolderLaunch())
            else:
                raise Refusal(f"unit {request.unit} resumes in {request.session.cwd}; its Units row names {existing.worktree}")
        request = replace(request, worktree=Path(existing.worktree), branch=existing.branch)
    launch = UnitLaunch(
        production=request.production,
        identity=UnitIdentity(request.unit, request.requested_name),
        branch=request.branch,
        worktree=request.worktree,
        plan=request.plan,
        port=request.port,
        owns=request.owns,
        session=request.session,
        launch_kind=request.launch_kind,
        timeout=request.timeout,
    )
    account = selected
    account_file = account.dir / ".claude.json"
    folder = request.session_cwd
    check_folder = (repository_trust_key(production.checkout)
                    if isinstance(request.session, NewSession) and not folder.exists() else folder)
    trust = claude_trust(check_folder, account_file)
    if isinstance(trust, UnreadableTrustFile):
        raise Refusal(f"{request.unit}: {trust}")
    if isinstance(trust, Untrusted):
        label = account.label
        raise Refusal(f"{request.unit}: Claude is not trusted in {folder} on {label}. Run: {trust_command(check_folder, account)}")
    if restoring or isinstance(launch.launch_kind, ResumeInStartFolderLaunch):
        # These launches adopt the row and need no worktree: how the unit was first added, whether
        # its Plan cell still names a live plan, and whether its worktree and branch are free
        # describe new work, not the session it resumes.
        return ReadyToLaunch(launch, existing, director_agent(launch), handoff)
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
    if isinstance(launch.plan, PlanGiven) and isinstance(plan_context, ProductionPlanRequired):
        result = git(production, "show", f"{production.merge_branch}:{launch.plan.path.as_posix()}", check=False)
        valid_plan = (_is_excluded_standing_plan(production, launch.plan.path) if result.returncode != 0
                      else any(line.startswith("> **Production:") for line in result.stdout.splitlines()))
        if not valid_plan:
            raise Refusal(f"plan {launch.plan.path} is missing on {production.merge_branch} or lacks the Production header")
    elif isinstance(launch.plan, BriefGiven) and (production.checkout / launch.plan.stub).exists():
        stub = production.checkout / launch.plan.stub
        content = stub.read_text(encoding="utf-8")
        source = content.split("## Source\n\n", 1)
        saved_words = source[1].split("\n\n", 1) if len(source) == 2 else []
        if (not any(line.startswith("> **Production:") for line in content.splitlines())
                or len(saved_words) != 2 or saved_words[1] != launch.plan.words + "\n"):
            raise Refusal(f"stub plan {launch.plan.stub} already exists with different content")
    return ReadyToLaunch(launch, existing, director_agent(launch), handoff)


def attach_source(session_id: str, pid: int) -> RunningSessionHandoff:
    """Require one live, attributed inventory identity before requesting its safe stop."""
    import inventory
    from record import session_records

    processes = sessions.session_processes(session_id)
    if isinstance(processes, sessions.SessionLivenessUnknown):
        raise Refusal(f"cannot attach {session_id}: {processes.reason}")
    if processes.pids != (pid,):
        raise Refusal(f"cannot attach {session_id}: expected one process; pids found: {list(processes.pids)}")
    matches = [entry for entry in inventory.listing() if entry.session_id == session_id and entry.pid == pid]
    identities = {(entry.login, entry.proc_start) for entry in matches}
    if len(identities) != 1:
        raise Refusal(f"cannot attach {session_id}: source account is unclear in the shutdown inventory")
    login, proc_start = identities.pop()
    if not login or not proc_start:
        raise Refusal(f"cannot attach {session_id}: source account or process identity is unreadable")
    if any(saved["session"]["session_id"] == session_id for saved in session_records(login)):
        raise Refusal(f"cannot attach {session_id}: a shutdown record already owns this session")
    return RunningSessionHandoff(session_id, pid, proc_start, login)


def run_attach_shutdown(arguments: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """The only stop boundary: the ordinary shutdown command, with its bounded follower."""
    return subprocess.run([sys.executable, str(SCRIPTS / "shutdown/shutdown.py"), *arguments],
                          env=launch_environment(), capture_output=True, text=True, check=False, timeout=timeout)


def stop_for_attach(handoff: RunningSessionHandoff) -> SessionShutdown:
    import restart
    import settle
    from record import session_records

    identity = str(uuid4())
    cancel = settle.shutdown_command("cancel", handoff.login, "--here", "--only", handoff.session_id)
    try:
        down = run_attach_shutdown(["down", handoff.login, "--here", "--only", handoff.session_id,
                                    "--request-id", identity], restart.SESSION_WAIT_SECONDS)
        if down.returncode != 0:
            raise Refusal(down.stderr.strip() or down.stdout.strip() or "shutdown down failed")
        followed = run_attach_shutdown(["follow", handoff.login, "--request", identity],
                                      settle.NOT_ANSWERING_AFTER.total_seconds() + settle.SETTLE_INTERVAL_SECONDS)
        if followed.returncode != 0:
            raise Refusal(followed.stderr.strip() or followed.stdout.strip() or "shutdown follow failed")
        records = [saved for saved in session_records(handoff.login)
                   if saved["session"]["session_id"] == handoff.session_id and saved["request_id"] == identity]
        if (len(records) != 1 or records[0]["progress"]["kind"] not in {"stopped", "already gone"}
                or records[0]["stop_issues"]):
            raise Refusal("shutdown record does not confirm a completed stop")
        return records[0]
    except (OSError, ValueError, subprocess.SubprocessError, Refusal) as error:
        raise Refusal(f"attach stop did not finish for {handoff.session_id}: {error}; to cancel: {cancel}") from error


def require_stopped_session(handoff: RunningSessionHandoff) -> None:
    processes = sessions.session_processes(handoff.session_id)
    if isinstance(processes, sessions.SessionLivenessUnknown):
        raise Refusal(f"cannot attach {handoff.session_id}: {processes.reason}")
    old = sessions.process_liveness(handoff.pid, handoff.proc_start)
    if isinstance(old, sessions.SessionLivenessUnknown):
        raise Refusal(f"cannot attach {handoff.session_id}: {old.reason}")
    if processes.pids or isinstance(old, sessions.LiveSessionProcess):
        pids = sorted(set(processes.pids) | ({old.pid} if isinstance(old, sessions.LiveSessionProcess) else set()))
        raise Refusal(f"cannot attach {handoff.session_id}: processes still run after shutdown; pids found: {pids}")


def attached_pid(handoff: RunningSessionHandoff) -> int:
    processes = sessions.session_processes(handoff.session_id)
    if isinstance(processes, sessions.SessionLivenessUnknown):
        raise Refusal(f"ATTACH UNPROVEN for {handoff.session_id}: {processes.reason}")
    if len(processes.pids) != 1 or handoff.pid in processes.pids:
        raise Refusal(f"ATTACH UNPROVEN for {handoff.session_id}: expected one new process; "
                      + f"pids found: {list(processes.pids)}; old pid: {handoff.pid}")
    return processes.pids[0]


def finish_attach(handoff: StoppedSessionHandoff,
                  selected: ClaudeLaunchAccount, outcome: herdr.PromptSubmitOutcome, timeout: float) -> int:
    import restart

    _ = restart.wait_for_confirmation(frozenset({handoff.session_id}), timeout)
    checked = restart.finish_attached_session(handoff.record, selected.label, outcome)
    pid = attached_pid(handoff.source)
    if checked.remaining or len(checked.finished) != 1:
        raise Refusal(f"attached {handoff.session_id} runs as pid {pid}, but its shutdown record did not finish")
    return pid


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
    if isinstance(request.plan, PlanGiven):
        destination = worktree / request.plan.path
        if not destination.exists() and _is_excluded_standing_plan(production, request.plan.path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = destination.write_bytes((production.checkout / request.plan.path).read_bytes())
    if (production.checkout / ".claude/config/berth.toml").exists():
        key = f"branch.{request.branch}.cargoBerthTarget"
        current = git(production, "config", "--get", key, check=False).stdout.strip()
        if current != production.merge_branch:
            _ = git(production, "config", key, production.merge_branch)


def prompt_for(request: UnitLaunch) -> str:
    production = request.production
    doc = str(production.doc)
    # The showrunner's address is written nowhere: a unit looks it up, since a restart changes it.
    # It is the session on this machine, so no other session of the same name can be meant.
    showrunner = ("the showrunner, whose `SendMessage` address `~/.claude/scripts/lib/py "
                  f"~/.claude/scripts/production/showrunners.py address {production.slug}` prints (look it up "
                  "before each message: it changes when the showrunner restarts)")
    if isinstance(request.plan, Standby):
        profile = request.profile
        kept = " You are permanent: wrap never retires you." if request.plan.permanent else ""
        place = (f" {profile.duty} Do nothing until it or the showrunner sends you work." if isinstance(profile, ProfiledLaunch)
                 else f" Work only in your worktree {request.worktree}, branch {request.branch}. "
                 + "Do nothing until the showrunner sends you work.")
        prompt = (f"You are {request.identity.unit} in production {production.slug} (doc {doc}), under "
                  f"{showrunner}, on standby.{kept}{place}")
        if isinstance(request.launch_kind, UnitRestoreLaunch):
            prompt += " " + request.launch_kind.note.read_text(encoding="utf-8").rstrip("\n")
        return prompt
    plan = request.plan_path.as_posix()
    if isinstance(request.session, ResumedSession):
        plan = str(request.worktree / request.plan_path)
        identity = (f"You are now {request.identity.unit} in production {production.slug} (doc {doc}), under "
                    f"{showrunner}. Work only in your worktree {request.worktree}, "
                    f"branch {request.branch}, and name in every Work Order the worktree and branch "
                    "git reports at dispatch.")
        if isinstance(request.launch_kind, UnitRestoreLaunch):
            note = request.launch_kind.note.read_text(encoding="utf-8").rstrip("\n")
            return f"{identity} {note}"
        return f"{identity} Run /unit:direct {plan}."
    if isinstance(request.plan, PlanGiven):
        return f"/unit:direct {plan}"
    return (f"You are {request.identity.unit} in production {production.slug} (doc {doc}), under "
            f"{showrunner}. Work only in your worktree {request.worktree}, "
            f"branch {request.branch}, and name in every Work Order the worktree and branch git reports "
            f"at dispatch. Your plan {plan} holds only the "
            f"user's words. Write the full phased plan there, send it to the showrunner, and wait for its "
            f"approval before you run /unit:direct {plan}.")


def attach_hint(request: UnitLaunch) -> str:
    """Where the user finds the unit: its tab in the production's workspace of the shared session."""
    import herdr

    production = request.production
    tab = (request.launch_kind.session_name if isinstance(request.launch_kind, UnitRestoreLaunch)
           else request.identity.session)
    return herdr.attach_hint(showrunners.herdr_space(production.slug, str(production.doc)), tab)


def host_session_name(request: UnitLaunch) -> str:
    """The destination name, limited to characters supported by named herdr sessions.

    A new launch's name leads with the production slug, since every production may have a unit of
    the same name (each had a review-unit). A restore keeps the name its session was recorded under
    when that name leads with the slug; a record from before the slug led, such as `review`, would
    take another production's live session of that name, so it gets the new launch's name instead.
    """
    import herdr

    slug = request.production.slug
    if isinstance(request.launch_kind, UnitRestoreLaunch):
        recorded = herdr.generated_session_name(request.launch_kind.host_session)
        if recorded.startswith(f"{slug}-"):
            return recorded
    name = request.identity.session
    return herdr.generated_session_name(unit_names.branch(slug, name))


def launch_session(
    request: UnitLaunch,
    director: DirectorAgent,
    prompt: str,
    selected: ClaudeLaunchAccount,
    *,
    target: str = "",
    model: str = "",
    autocompact_window: str = "",
) -> herdr.PromptSubmitOutcome:
    """Start Claude in a tab of the shared herdr session, carrying this unit's marks."""
    import herdr
    from scripts.accounts import accounts
    from scripts.whoami.account import named_account_environment

    environment = {unit_lookup.PRODUCTION_MARK: request.production.slug,
                   unit_lookup.UNIT_MARK: request.identity.unit, "ENABLE_TOOL_SEARCH": "true"}
    if target:
        if target != selected.label:
            raise Refusal("resume target must match the selected group account")
        prepared = accounts.prepared_account(selected.label, resume=True)
        if isinstance(prepared, accounts.AccountRefused):
            raise Refusal(prepared.reason)
        environment.update(prepared.variables)
        for key in prepared.remove:
            _ = environment.pop(key, None)
    else:
        environment.update(named_account_environment(selected.dir))
    arguments = ["--model", model or director.model]
    if isinstance(director.effort, Effort):
        arguments.extend(["--effort", director.effort.value])
    if isinstance(request.session, ResumedSession):
        session_id = request.session.session_id
        arguments.extend(["--resume", session_id])
    else:
        session_id = str(uuid4())
        arguments.extend(["--session-id", session_id])
    if autocompact_window:
        arguments.extend(["--autocompact", autocompact_window])
    session_name = (request.launch_kind.session_name if isinstance(request.launch_kind, UnitRestoreLaunch)
                    else request.identity.session)
    profile = request.profile
    flags = profile.flags if isinstance(profile, ProfiledLaunch) else ("--settings", '{"disableAgentView": true}')
    if isinstance(request.launch_kind, UnitRestoreLaunch) and request.launch_kind.probe:
        environment[PROBE_ENV] = "1"
    arguments.extend(["--remote-control", session_name, "-n", session_name, *flags])
    timeout_ms = int(request.timeout * 1000)
    host = herdr.shared_session()
    _ = herdr.start_server(host, timeout_ms)
    production = request.production
    launched = herdr.launch_agent(host, showrunners.herdr_space(production.slug, str(production.doc)),
        session_name, str(request.session_cwd), environment, kind="claude", agent=host_session_name(request),
        agent_arguments=arguments, opening=herdr.OpeningPrompt(prompt, herdr.SessionTranscript(selected.dir, session_id)),
        timeout_ms=timeout_ms)
    if isinstance(launched, herdr.nix_env.Refused):
        raise Refusal(launched.reason)
    outcome = launched.outcome
    return outcome if isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted)) else herdr.PromptNotSubmitted("launcher returned no submit proof")


def close_untyped_session(request: UnitLaunch, outcome: herdr.PromptNotSubmitted) -> str:
    """Close a new session that never got its prompt, so no half-started unit stays behind.

    The Units row, commit and worktree stay: the same command finds them and starts the session
    again. A prompt typed even once may have started work, so that session stays open.
    """
    import herdr

    if not outcome.never_typed:
        return ""
    found = launched_unit(request)
    if isinstance(found, NoMarkedUnit):
        return "; run the same command again"
    try:
        herdr.close_agent(found.unit.place.session, found.unit.place.pane)
    except herdr.HerdrFailure as error:
        return f"; its session {found.unit.place.session} could not be closed: {error}"
    return "; its session is closed: run the same command again"


def launch_environment() -> dict[str, str]:
    """The sanitized caller environment used by the launch-permission check."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CLAUDE_")
    }


def require_shutdown_launch_permission(selected: ClaudeLaunchAccount, purpose: launch_permission.LaunchPurpose) -> None:
    import launch_permission

    environment = launch_environment()
    environment["CLAUDE_CONFIG_DIR"] = str(selected.dir)
    permission = launch_permission.launch_permission(environment, purpose)
    if isinstance(permission, launch_permission.BlockedByShutdown):
        raise Refusal(f"{permission.reason}; /shutdown restart first")
    if isinstance(permission, launch_permission.ShutdownStateUnreadable):
        raise Refusal(f"shutdown state unreadable: {permission.detail}")


def record_scheduled_restore_prompt(session_id: str, prompt: str) -> None:
    # Imported here, not at the top: live_units.py and update_registration.py
    # import this module for its table readers from trees without scripts/hooks.
    import conversation_pause

    try:
        recorded = conversation_pause.read_scheduled_prompts(session_id)
        conversation_pause.record_scheduled_prompts(session_id, (*recorded, prompt))
    except (OSError, RuntimeError, ValueError) as error:
        detail = str(error).strip()
        reason = detail.splitlines()[0] if detail else type(error).__name__
        raise RuntimeError(f"restart prompt not recorded: {reason}") from error


def launched_unit(request: UnitLaunch | RequestedUnitLaunch) -> MarkedUnitFound | NoMarkedUnit:
    """The unit's session, found by its mark."""
    unit = unit_lookup.marked_unit(
        request.production.slug, request.unit if isinstance(request, RequestedUnitLaunch) else request.identity.unit)
    return NoMarkedUnit() if unit is None else MarkedUnitFound(unit)


def registry_remote_control(claude: unit_lookup.Claude) -> bool:
    """Remote Control is on when this session's registry record carries a bridge session.

    The record does not scroll off, as the pane's "/remote-control is active" line does
    once the opening prompt starts work.
    """
    import sessions

    if not isinstance(claude, unit_lookup.LiveClaude):
        return False
    record = sessions.read_session(sessions.sessions_dir() / f"{claude.pid}.json")
    if isinstance(record, sessions.UnreadableSessionRecord) or record["sessionId"] != claude.session_id:
        return False
    return sessions.remote_control(record) == "active"


def wait_for_remote_control(request: UnitLaunch) -> unit_lookup.MarkedUnit:
    from session_pane import HerdrFailure, read_place

    found = launched_unit(request)
    if isinstance(found, NoMarkedUnit):
        raise RuntimeError(f"{request.identity.unit}: no session carries its mark")
    launched = found.unit
    deadline = time.monotonic() + request.timeout
    pane = ""
    while True:
        if not isinstance(launched.claude, unit_lookup.LiveClaude):
            # The session record can lag the pane; look again until the mark names a live Claude.
            try:
                again = launched_unit(request)
            except OSError:
                again = found
            if isinstance(again, MarkedUnitFound):
                launched = again.unit
        if registry_remote_control(launched.claude):
            return launched
        try:
            pane = read_place(launched.place)
        except (OSError, HerdrFailure) as error:
            pane = str(error)
        if "/remote-control is active" in pane:
            return launched
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = "\n".join(pane.splitlines()[-15:])
            raise RuntimeError(f"{request.identity.unit} (herdr {launched.place.session}:{launched.place.pane}): /remote-control did not become active\n{tail}")
        time.sleep(min(0.5, remaining))


def record(request: UnitLaunch) -> None:
    production = request.production
    log_line = f"added {request.identity.unit} ({request.mode_name}), worktree {request.worktree}"
    if production.log.exists() and any(log_line in line for line in production.log.read_text(encoding="utf-8").splitlines()):
        return
    stamp = datetime.now(production.zone).strftime("%H:%M %Z")
    with production.log.open("a", encoding="utf-8") as log:
        _ = log.write(f"- {stamp}: {log_line}\n")


def account_for_launch(request: RequestedUnitLaunch, target: str = "", config_dir: str = "",
                       *, source_fallback: bool = False) -> ClaudeLaunchAccount:
    """Use the units account, or the recorded source for a failed move."""
    from scripts.accounts import accounts
    try:
        if source_fallback:
            from record import all_session_records

            if not isinstance(request.launch_kind, UnitRestoreLaunch):
                raise Refusal("--source-fallback requires a recorded unit restore")
            identity = request.launch_kind.session_id
            matches = [saved for saved in all_session_records()
                       if saved["session"]["session_id"] == identity
                       and saved["session"]["kind"] == "unit" and saved.get("target") == target
                       and saved.get("source_label", saved["label"]) == target]
            if len(matches) != 1:
                raise Refusal("--source-fallback must name the shutdown record's source account")
        return accounts.account_for_group_launch("unit", resume=isinstance(request.session, ResumedSession),
                                                 target=target, config_dir=config_dir, source_fallback=source_fallback)
    except accounts.AccountsError as error:
        raise Refusal(str(error)) from error


class TranscriptMessage(TypedDict, total=False):
    model: str


class TranscriptEntry(TypedDict, total=False):
    type: str
    message: TranscriptMessage


def transcript_lines_from_end(path: Path) -> Iterator[str]:
    """Read newest entries first without loading the whole conversation."""
    with path.open("rb") as stream:
        offset = stream.seek(0, os.SEEK_END)
        pending = b""
        while offset:
            size = min(offset, 65536)
            offset -= size
            _ = stream.seek(offset)
            lines = (stream.read(size) + pending).split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                yield line.decode("utf-8", errors="replace")
        if pending:
            yield pending.decode("utf-8", errors="replace")


def transcript_model(session_id: str) -> str:
    """The newest real assistant model in the newest transcript for this session."""
    try:
        matches = list((Path.home() / ".claude/projects").rglob(f"{session_id}.jsonl"))
        if not matches:
            return ""
        path = max(matches, key=lambda path: path.stat().st_mtime)
        for line in transcript_lines_from_end(path):
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            entry = cast(TranscriptEntry, cast(object, value))
            message = cast(object, entry.get("message"))
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            model = cast(object, cast(TranscriptMessage, cast(object, message)).get("model", ""))
            if isinstance(model, str) and model and model != "<synthetic>":
                return model
    except OSError:
        pass
    return ""


def model_for_resume(request: RequestedUnitLaunch, explicit: str = "") -> str:
    """Use the recorded model when available, with --model as the restore boundary."""
    if isinstance(request.session, NewSession):
        if explicit:
            raise Refusal("--model requires --resume")
        return ""
    if explicit:
        return explicit
    import record as shutdown_record
    try:
        saved = [item for item in shutdown_record.all_session_records()
                 if item["session"]["session_id"] == request.session.session_id]
    except (shutdown_record.InvalidRecord, OSError) as error:
        raise Refusal(f"shutdown state unreadable: {error}") from error
    if len(saved) == 1 and saved[0]["session"]["model"]["kind"] == "model":
        return saved[0]["session"]["model"]["name"]
    return transcript_model(request.session.session_id) if isinstance(launched_unit(request), MarkedUnitFound) else ""


def autocompact_for_resume(request: RequestedUnitLaunch, explicit: str = "") -> str:
    """The session's recorded auto-compact window, with --autocompact as the value to record."""
    if isinstance(request.session, NewSession):
        if explicit:
            raise Refusal("--autocompact requires --resume")
        return ""
    try:
        if explicit:
            value = autocompact.checked(explicit)
            return "" if value == autocompact.AUTO else value
        return autocompact.recorded(request.session.session_id)
    except (autocompact.BadAutocompact, OSError, UnicodeError) as error:
        raise Refusal(f"auto-compact record: {error}") from error


def registered_unit(request: RequestedUnitLaunch) -> bool:
    lines = request.production.doc.read_text(encoding="utf-8").splitlines()
    return any(cell_value(cells.get("Unit", "")) == request.unit for cells in unit_table(lines))


def set_autocompact(request: RequestedUnitLaunch, session_id: str, value: str, check: bool) -> None:
    """Record a registered unit's window for its next restart; a running unit is not relaunched."""
    try:
        value = autocompact.checked(value)
        if not check:
            autocompact.record(session_id, value)
    except (autocompact.BadAutocompact, OSError, UnicodeError) as error:
        raise Refusal(f"auto-compact record: {error}") from error
    verb = "would apply" if check else "applies"
    setting = "the saved setting" if value == autocompact.AUTO else f"auto-compact {value}"
    print(f"{request.unit}: {setting} {verb} to session {session_id} at its next restart")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("name")
    _ = parser.add_argument("--plan")
    _ = parser.add_argument("--brief")
    _ = parser.add_argument("--standby", action="store_true")
    _ = parser.add_argument("--permanent", action="store_true")
    _ = parser.add_argument("--port", type=int)
    _ = parser.add_argument("--owns")
    _ = parser.add_argument("--timeout", type=float, default=90.0)
    conversation = parser.add_mutually_exclusive_group()
    _ = conversation.add_argument("--resume")
    _ = conversation.add_argument("--attach", help="safely stop a running session and resume it as this unit")
    _ = parser.add_argument("--account", default="")
    _ = parser.add_argument("--source-fallback", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--model", default="")
    _ = parser.add_argument("--autocompact", default="", help="auto or 100k-1M; kept for every relaunch of --resume; a listed unit only records it")
    _ = parser.add_argument("--config-dir", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--cwd")
    _ = parser.add_argument("--restart-note")
    _ = parser.add_argument("--session-name")
    _ = parser.add_argument("--host-session")
    _ = parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    import herdr
    try:
        request = launch_request(args)
        explicit_autocompact = cast(str, args.autocompact)
        if (explicit_autocompact and isinstance(request.session, ResumedSession)
                and isinstance(request.session.handoff, ResumeStoppedSession)
                and isinstance(request.launch_kind, NewWorkLaunch) and registered_unit(request)):
            # Setting a registered unit's window is not new work: no new-work check or launch applies.
            set_autocompact(request, request.session.session_id, explicit_autocompact, cast(bool, args.check))
            return 0
        # Table readers import this module from copied trees without accounts.
        from scripts.accounts import accounts

        unit_account = account_for_launch(request, cast(str, args.account), cast(str, args.config_dir),
                                          source_fallback=cast(bool, args.source_fallback))
        problems = accounts.check(unit_account.label)
        if problems:
            raise Refusal(
                f"unit account {unit_account.label}: {problems[0]}; /showrunner:accounts"
            )
        resume_model = model_for_resume(request, cast(str, args.model))
        resume_autocompact = autocompact_for_resume(request, cast(str, args.autocompact))
        # Read the shutdown launch barrier only when a unit is launched.
        import launch_permission

        handoff: NoSessionHandoff | StoppedSessionHandoff = NoSessionHandoff()
        if isinstance(request.session, ResumedSession) and isinstance(request.session.handoff, AttachRunningSession):
            # Release the launch barrier before down creates its own shutdown record.
            with launch_permission.launch_barrier():
                require_shutdown_launch_permission(unit_account, launch_permission.NewWorkLaunchPurpose())
                attach_ready = preflight(request, unit_account)
            source = attach_ready.handoff
            if cast(bool, args.check):
                return 0
            assert isinstance(source, RunningSessionHandoff)
            saved = stop_for_attach(source)
            require_stopped_session(source)
            handoff = StoppedSessionHandoff(source, saved)
            request = replace(request, session=request.session._replace(handoff=ResumeStoppedSession()))

        barrier = launch_permission.launch_barrier()
        try:
            _ = barrier.__enter__()
        except OSError as error:
            raise Refusal(
                f"shutdown state unreadable: launch barrier: {error}"
            ) from error
        outcome = herdr.PromptNotSubmitted("restore reused a session without submit proof")
        try:
            purpose: launch_permission.LaunchPurpose = (
                launch_permission.RecordedSessionRestoreLaunchPurpose(
                    request.launch_kind.session_id
                )
                if isinstance(request.launch_kind, UnitRestoreLaunch)
                else launch_permission.RecordedSessionRestoreLaunchPurpose(handoff.session_id)
                if isinstance(handoff, StoppedSessionHandoff)
                else launch_permission.NewWorkLaunchPurpose()
            )
            require_shutdown_launch_permission(unit_account, purpose)

            ready = preflight(request, unit_account)
            request = ready.launch
            if cast(bool, args.check):
                return 0
            if cast(str, args.autocompact) and isinstance(request.session, ResumedSession):
                autocompact.record(request.session.session_id, cast(str, args.autocompact))
            marked_unit = launched_unit(request)
            if (
                isinstance(request.session, ResumedSession)
                and isinstance(marked_unit, MarkedUnitFound)
            ):
                unit = marked_unit.unit
                if isinstance(unit.claude, unit_lookup.ClaudeUnknown):
                    raise Refusal(
                        f"cannot tell whether Claude runs in session {unit.place.session}: "
                        + unit.claude.reason
                    )
                if isinstance(unit.claude, unit_lookup.ClaudeNotRunning):
                    try:
                        herdr.close_agent(unit.place.session, unit.place.pane)
                    except herdr.HerdrFailure as error:
                        raise Refusal(f"cannot end session {unit.place.session}: {error}") from error
                    marked_unit = NoMarkedUnit()
            if isinstance(handoff, StoppedSessionHandoff) and isinstance(marked_unit, MarkedUnitFound):
                raise Refusal(f"cannot attach {handoff.session_id}: unit tab already runs")
            if not isinstance(request.launch_kind, ResumeInStartFolderLaunch):
                write_stub(request)
                append_row(request, ready.row)
                commit_unit(request)
            # A restore or a retired unit's external start folder needs no worktree or branch.
            if not isinstance(request.launch_kind, (UnitRestoreLaunch, ResumeInStartFolderLaunch)):
                ensure_worktree(request)
            if isinstance(marked_unit, NoMarkedUnit):
                prompt = prompt_for(request)
                if isinstance(request.launch_kind, UnitRestoreLaunch):
                    record_scheduled_restore_prompt(
                        request.launch_kind.session_id, prompt
                    )
                outcome = launch_session(request, ready.director, prompt, unit_account, target=unit_account.label
                               if isinstance(request.session, ResumedSession) else "", model=resume_model,
                               autocompact_window=resume_autocompact)
        finally:
            _ = barrier.__exit__(None, None, None)
        if isinstance(request.launch_kind, UnitRestoreLaunch):
            print("restart-prompt-outcome: " + herdr.encode_prompt_outcome(outcome))
            if isinstance(outcome, herdr.PromptNotSubmitted):
                raise RuntimeError(f"{request.identity.unit}: restart prompt not submitted: {outcome.reason}")
            record(request)
            print(f"{request.identity.unit} restore launched: {attach_hint(request)}")
            return 0
        if isinstance(outcome, herdr.PromptNotSubmitted) and isinstance(marked_unit, NoMarkedUnit):
            if isinstance(handoff, StoppedSessionHandoff):
                raise Refusal(f"ATTACH UNPROVEN for {handoff.session_id}: opening prompt not submitted: {outcome.reason}")
            raise RuntimeError(f"{request.identity.unit}: opening prompt not submitted: {outcome.reason}"
                               + close_untyped_session(request, outcome))
        if isinstance(handoff, StoppedSessionHandoff):
            pid = finish_attach(handoff, unit_account, outcome, request.timeout)
            record(request)
            print(f"attached {handoff.session_id} as {request.identity.unit}: one process, pid {pid}")
            return 0
        _ = wait_for_remote_control(request)
        record(request)
        print(f"{request.identity.unit} started: {attach_hint(request)}")
        return 0
    except herdr.AgentNotReady as error:
        print(f"add_unit: the session is not ready:\n{error.screen}", file=sys.stderr)
        return 1
    except Refusal as error:
        print(f"add_unit: {error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, subprocess.SubprocessError, RuntimeError, ValueError, herdr.HerdrFailure) as error:
        detail = str(error)
        print(f"add_unit: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
