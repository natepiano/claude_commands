#!/usr/bin/env python3
"""Add one unit to a running production, with restartable launch steps."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


import argparse
import json
from collections.abc import Iterator
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast, override

from scripts.production import staff, unit_lookup, staff_sources, unit_config, session_staff
from scripts.herdr import herdr
from scripts.shutdown import remote
from scripts.lib import git_exclude

SCRIPTS = Path(__file__).resolve().parents[1]
from scripts.production import autocompact
from scripts.message import sessions
from scripts.production import unit_names
from scripts.production.production_doc import Production, Refusal, read_production

from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_sources import ensure_staff_showrunner as ensure_staff_showrunner

if TYPE_CHECKING:
    from scripts.accounts.accounts import ClaudeLaunchAccount


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


def launch_profile(responsibilities: tuple[str, ...]) -> LaunchProfile:
    """The nightly-review unit reads every repository from ~/rust under its own read-only settings."""
    if "nightly-review" not in responsibilities:
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
    """The session stop completed; its old identity and registry are clear."""

    source: RunningSessionHandoff

    @property
    def session_id(self) -> str:
        return self.source.session_id


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
    """The unit has no .unit.toml yet: this launch admits it."""


class ExistingUnitRow(NamedTuple):
    """An admitted unit: its staff worktree, the branch there and its .unit.toml plan."""

    plan: str
    worktree: Path
    branch: str


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
    session: NewSession | ResumedSession
    timeout: float
    responsibilities: tuple[str, ...] = ()

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
        return launch_profile(self.responsibilities)

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


class NoPlanTransition(NamedTuple):
    """The launch keeps its mode or adopts an existing conversation."""


class BriefToPlanTransition(NamedTuple):
    """The launch replaces a brief and must record plan mode."""


class ReadyToLaunch(NamedTuple):
    launch: UnitLaunch
    row: NoUnitRow | ExistingUnitRow
    director: DirectorAgent
    handoff: NoSessionHandoff | RunningSessionHandoff = NoSessionHandoff()
    plan_transition: NoPlanTransition | BriefToPlanTransition = NoPlanTransition()


@dataclass(frozen=True)
class LabeledSessionFound:
    """The unit has a session with its native runtime identity."""

    unit: unit_lookup.LabeledSession


@dataclass(frozen=True)
class NoLabeledSession:
    """No observed session has the requested unit identity."""


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


def launch_request(args: argparse.Namespace, *,
                   record: staff.StaffSnapshot | None = None) -> RequestedUnitLaunch:
    record = staff.initial_snapshot() if record is None else record
    production = read_production(cast(str, args.production), record=record)
    try:
        name = unit_names.name(cast(str, args.name))
    except unit_names.BadName as error:
        raise Refusal(str(error)) from error
    given_plan = cast(str | None, args.plan)
    given_brief = cast(str | None, args.brief)
    standby = cast(bool, args.standby)
    permanent = cast(bool, args.permanent)
    resumed = cast(str | None, args.resume)
    attached = cast(str | None, getattr(args, "attach", None))
    if (not any((given_plan is not None, given_brief is not None, standby, permanent))
            and (resumed is not None or attached is not None)):
        member = checked_unit_member(production, name, record,
                                     identity=session_staff.LiveSession(resumed or attached or ""))
        if not isinstance(member, staff_sources.MemberFound) or not isinstance(member.row.worktree, staff.PresentWorktree):
            raise Refusal(f"unit {name}: resume needs a filled present worktree")
        if member.row.worktree.machine != remote.local_machine():
            raise Refusal(f"unit {name}: resume worktree is on {member.row.worktree.machine}")
        saved = unit_config.read(member.row.worktree.path, cleanup=False)
        if isinstance(saved, unit_config.ConfigMissing):
            raise Refusal(saved.reason)
        if isinstance(saved.plan, unit_config.PlanFile):
            given_plan = str(saved.plan.path)
        else:
            standby = isinstance(saved.plan, unit_config.StandbyPlan)
            permanent = isinstance(saved.plan, unit_config.PermanentPlan)
    if sum((given_plan is not None, given_brief is not None, standby, permanent)) != 1:
        raise Refusal("give exactly one of --plan, --brief, --standby, or --permanent")
    kebab = unit_names.stem(name)
    branch = unit_names.branch(production.name, kebab)
    worktree = unit_names.worktree(production.checkout, kebab)
    plan: PlanGiven | BriefGiven | Standby
    if given_plan is not None:
        plan = PlanGiven(relative_plan(production, given_plan))
    elif given_brief is not None:
        assert given_brief is not None
        plan = BriefGiven(given_brief, stub_plan(production, branch))
    else:
        plan = Standby(permanent)
    identity = attached if attached is not None else resumed
    session_flag = "--attach" if attached is not None else "--resume"
    cwd = cast(str | None, args.cwd)
    if (identity is None) != (cwd is None):
        raise Refusal(f"{session_flag} and --cwd must be given together")
    if identity is not None and isinstance(plan, BriefGiven):
        raise Refusal(f"{session_flag} requires --plan, --standby, or --permanent")
    session: NewSession | ResumedSession = (
        ResumedSession(identity, Path(cwd).expanduser().resolve(),
                       AttachRunningSession() if attached is not None else ResumeStoppedSession())
        if identity is not None and cwd is not None else NewSession()
    )
    port = cast(int | None, args.port)
    timeout = cast(float, args.timeout)
    if port is not None and not 1 <= port <= 65535:
        raise Refusal("--port must be between 1 and 65535")
    if timeout < 0:
        raise Refusal("--timeout must be nonnegative")
    return RequestedUnitLaunch(
        production=production,
        requested_name=name,
        unit=name,
        branch=branch,
        worktree=worktree,
        plan=plan,
        port=SuppliedCell(str(port)) if port is not None else OmittedCell(),
        session=session,
        timeout=timeout,
        responsibilities=tuple(cast(list[str], getattr(args, "responsibility", None) or [])),
    )


def plan_text(config: unit_config.UnitConfig) -> str:
    plan = config.plan
    if isinstance(plan, unit_config.PlanFile):
        return plan.path.as_posix()
    return "standby" if isinstance(plan, unit_config.StandbyPlan) else "permanent"


def row_is_present(request: RequestedUnitLaunch, member: staff_sources.MemberFound | staff_sources.NoSuchMember,
                   record: staff.StaffSnapshot) -> NoUnitRow | ExistingUnitRow:
    """Read an admitted unit from its staff worktree; a resumed session keeps that worktree and branch."""
    holder = record.unit_by_worktree(remote.local_machine(), request.worktree)
    if holder is not None and (isinstance(member, staff_sources.NoSuchMember) or holder.id != member.row.id):
        raise Refusal(f"worktree {request.worktree} for {request.unit} is already used by {holder.name}")
    worktree = (unit_lookup.local_worktree(member).path if isinstance(member, staff_sources.MemberFound)
                else request.worktree)
    config = unit_config.read(worktree, cleanup=False)
    if isinstance(config, unit_config.ConfigMissing):
        return NoUnitRow()
    branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                            text=True, capture_output=True, check=False).stdout.strip()
    existing = ExistingUnitRow(plan_text(config), worktree, branch)
    if not isinstance(request.session, ResumedSession):
        for cell, actual, expected in (("Worktree", str(worktree), str(request.worktree)),
                                       ("Branch", branch, request.branch)):
            if actual != expected:
                raise Refusal(f"unit {request.unit} {cell} is {actual!r}; expected {expected!r}")
    return existing


def recorded_mode(request: UnitLaunch, plan: str) -> str:
    subject_prefix = f"production({request.production.name}): add unit {request.identity.unit} ("
    subjects = git(request.production, "log", "--format=%s", "--fixed-strings",
                   f"--grep={subject_prefix}", request.production.merge_branch).stdout.splitlines()
    for subject in subjects:
        if subject in (subject_prefix + "plan)", subject_prefix + "brief)", subject_prefix + "standby)",
                       subject_prefix + "permanent)"):
            return subject.removeprefix(subject_prefix).removesuffix(")")
    if plan in ("standby", "permanent"):
        return plan
    stub = stub_plan(request.production, request.branch)
    if plan == stub.as_posix():
        path = request.production.checkout / stub
        # A missing stub is a brief whose stub write stopped after its config was published.
        if not path.exists() or "## Source\n\n" in path.read_text(encoding="utf-8"):
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
    resolver = Path(__file__).resolve().parents[1] / "agents" / "models_config.sh"
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
    return (any(line.startswith("> **Standing:**") for line in lines)
            and git(production, "check-ignore", "-q", "--", plan.as_posix(), check=False).returncode == 0)


def preflight(request: RequestedUnitLaunch, selected: ClaudeLaunchAccount, *,
              record: staff.StaffSnapshot | None = None) -> ReadyToLaunch:
    del selected

    handoff: NoSessionHandoff | RunningSessionHandoff = NoSessionHandoff()
    resuming_opening = False
    if isinstance(request.session, ResumedSession):
        session_id = request.session.session_id
        observed = sessions.session_liveness(session_id)
        resuming_opening = (isinstance(observed, sessions.LiveSessionProcess)
                            and pending_opening(request.production, request.unit, session_id))
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise Refusal(f"cannot tell whether session {session_id} runs: {observed.reason}")
        if isinstance(request.session.handoff, AttachRunningSession):
            if isinstance(observed, sessions.SessionNotLive):
                raise Refusal(f"session {session_id} does not run; use --resume")
            handoff = attach_source(session_id, observed.pid)
        elif (isinstance(observed, sessions.LiveSessionProcess)
              and not resuming_opening):
            raise Refusal(f"session {session_id} already runs as pid {observed.pid}; "
                          + f"to make it this unit, run add_unit with --attach {session_id}")
    production = request.production
    record = staff.initial_snapshot() if record is None else record
    member = checked_unit_member(production, request.unit, record,
                                 identity=(session_staff.LiveSession(request.session.session_id)
                                           if isinstance(request.session, ResumedSession) else session_staff.NO_SESSION))
    branch = git(production, "branch", "--show-current").stdout.strip()
    if branch != production.merge_branch:
        raise Refusal(f"Showrunner checkout is not on {production.merge_branch}")
    try:
        _ = production.doc.relative_to(production.checkout)
    except ValueError as error:
        raise Refusal("production doc must be inside Showrunner checkout") from error
    existing = row_is_present(request, member, record)
    plan_context: ProductionPlanRequired | ExistingConversationPlan = ProductionPlanRequired()
    if (isinstance(existing, ExistingUnitRow)
            and isinstance(request.session, ResumedSession)
            and isinstance(request.session.handoff, ResumeStoppedSession)
            and request.plan_cell == existing.plan):
        plan_context = ExistingConversationPlan()
    if isinstance(request.session, ResumedSession) and isinstance(existing, ExistingUnitRow):
        if request.session.cwd != existing.worktree.resolve() and not resuming_opening:
            raise Refusal(f"unit {request.unit} resumes in {request.session.cwd}; its staff row names {existing.worktree}")
        request = replace(request, worktree=existing.worktree, branch=existing.branch)
    launch = UnitLaunch(
        production=request.production,
        identity=UnitIdentity(request.unit, request.requested_name),
        branch=request.branch,
        worktree=request.worktree,
        plan=request.plan,
        port=request.port,
        session=request.session,
        timeout=request.timeout,
        responsibilities=request.responsibilities,
    )
    if launch.worktree.exists():
        git_exclude.require_untracked_ignored(launch.worktree, ".unit.toml", planned_pattern="/.unit.toml")
    plan_transition: NoPlanTransition | BriefToPlanTransition = NoPlanTransition()
    if isinstance(existing, ExistingUnitRow):
        mode = recorded_mode(launch, existing.plan)
        if mode != launch.mode_name and not (mode == "brief" and isinstance(launch.plan, PlanGiven)):
            retry = ["add_unit.py", "--production", production.name, launch.identity.unit, f"--{mode}"]
            if mode == "plan":
                retry.append(existing.plan)
            elif mode == "brief":
                retry.append("<original words>")
            raise Refusal(f"unit {launch.identity.unit} was added in {mode} mode; "
                          + f"{launch.mode_name} mode cannot reuse its row; run {shlex.join(retry)}")
        if mode == launch.mode_name and existing.plan != launch.plan_cell:
            raise Refusal(f"unit {request.unit} Plan is {existing.plan!r}; expected {launch.plan_cell!r}")
        if mode == "brief" and isinstance(launch.plan, PlanGiven):
            plan_transition = BriefToPlanTransition()
    launch = replace(launch, responsibilities=configuration_for(launch).responsibilities)
    has_worktree = worktree_on_branch(launch)
    local_branch = git(production, "show-ref", "--verify", "--quiet",
                       f"refs/heads/{request.branch}", check=False).returncode == 0
    if (local_branch or remote_head(production, request.branch)) and not has_worktree:
        raise Refusal(f"branch {request.branch} already exists")
    if isinstance(launch.plan, PlanGiven) and isinstance(plan_context, ProductionPlanRequired):
        result = git(production, "show", f"{production.merge_branch}:{launch.plan.path.as_posix()}", check=False)
        valid_plan = result.returncode == 0 or _is_excluded_standing_plan(production, launch.plan.path)
        if not valid_plan:
            raise Refusal(f"plan {launch.plan.path} is missing on {production.merge_branch}")
    elif isinstance(launch.plan, BriefGiven) and (production.checkout / launch.plan.stub).exists():
        stub = production.checkout / launch.plan.stub
        content = stub.read_text(encoding="utf-8")
        source = content.split("## Source\n\n", 1)
        saved_words = source[1].split("\n\n", 1) if len(source) == 2 else []
        if len(saved_words) != 2 or saved_words[1] != launch.plan.words + "\n":
            raise Refusal(f"stub plan {launch.plan.stub} already exists with different content")
    return ReadyToLaunch(launch, existing, director_agent(launch), handoff, plan_transition)


def attach_source(session_id: str, pid: int) -> RunningSessionHandoff:
    """Require one live, attributed inventory identity before requesting its safe stop."""
    from scripts.shutdown import inventory

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
    return RunningSessionHandoff(session_id, pid, proc_start, login)


def stop_for_attach(handoff: RunningSessionHandoff) -> None:
    from scripts.shutdown import stop_resume

    result = stop_resume.stop(handoff.session_id)
    if not isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
        reason = result.reason if isinstance(result, stop_resume.StopFailed) else "no local session tab"
        raise Refusal(f"attach stop did not finish for {handoff.session_id}: {reason}")


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
    del selected
    from scripts.shutdown import restart

    if isinstance(outcome, herdr.PromptNotSubmitted):
        raise Refusal(f"ATTACH UNPROVEN for {handoff.session_id}: {outcome.reason}")
    confirmed = restart.wait_for_confirmation(frozenset({handoff.session_id}), timeout)
    if handoff.session_id not in confirmed.live_ids:
        raise Refusal(f"ATTACH UNPROVEN for {handoff.session_id}: session did not return")
    return attached_pid(handoff.source)


def write_stub(request: UnitLaunch) -> None:
    if not isinstance(request.plan, BriefGiven):
        return
    path = request.production.checkout / request.plan.stub
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(request.production.zone).strftime("%Y-%m-%d %H:%M %Z")
    header = f"# {request.identity.session}\n\n"
    source = f"## Source\n\n{stamp}\n\n{request.plan.words}\n"
    write_text_atomic(path, header + source)


def remote_head(production: Production, branch: str) -> str:
    result = git(production, "ls-remote", "--heads", "origin", branch)
    return result.stdout.split()[0] if result.stdout.strip() else ""


def commit_paths(production: Production, paths: list[Path], message: str, *, allow_empty: bool = False) -> bool:
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
    elif allow_empty:
        _ = git(production, "commit", "--allow-empty", "--only", "-m", message)
    else:
        return False
    return True


def commit_unit(request: UnitLaunch,
                plan_transition: NoPlanTransition | BriefToPlanTransition) -> None:
    """Commit a brief's stub plan, or record a brief's switch to its plan, on the merge branch."""
    message = f"production({request.production.name}): add unit {request.identity.unit} ({request.mode_name})"
    if isinstance(request.plan, BriefGiven):
        _ = commit_paths(request.production, [request.production.checkout / request.plan.stub], message)
    elif isinstance(plan_transition, BriefToPlanTransition):
        _ = git(request.production, "commit", "--allow-empty", "--only", "-m", message)


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
        if not destination.exists():
            if _is_excluded_standing_plan(production, request.plan.path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(destination, (production.checkout / request.plan.path).read_text(encoding="utf-8"))
            else:
                _ = git(production, "-C", str(worktree), "restore", f"--source={production.merge_branch}",
                        "--worktree", "--", request.plan.path.as_posix())
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
                  f"~/.claude/scripts/production/showrunners.py address {production.name}` prints (look it up "
                  "before each message: it changes when the showrunner restarts)")
    if isinstance(request.plan, Standby):
        profile = request.profile
        kept = " You are permanent: wrap never retires you." if request.plan.permanent else ""
        place = (f" {profile.duty} Do nothing until it or the showrunner sends you work." if isinstance(profile, ProfiledLaunch)
                 else f" Work only in your worktree {request.worktree}, branch {request.branch}. "
                 + "Do nothing until the showrunner sends you work.")
        prompt = (f"You are {request.identity.unit} in production {production.name} (doc {doc}), under "
                  f"{showrunner}, on standby.{kept}{place}")
        return prompt
    plan = request.plan_path.as_posix()
    if isinstance(request.session, ResumedSession):
        plan = str(request.worktree / request.plan_path)
        identity = (f"You are now {request.identity.unit} in production {production.name} (doc {doc}), under "
                    f"{showrunner}. Work only in your worktree {request.worktree}, "
                    f"branch {request.branch}, and name in every Work Order the worktree and branch "
                    "git reports at dispatch.")
        return f"{identity} Run /unit:direct {plan}."
    if isinstance(request.plan, PlanGiven):
        return f"/unit:direct {plan}"
    return (f"You are {request.identity.unit} in production {production.name} (doc {doc}), under "
            f"{showrunner}. Work only in your worktree {request.worktree}, "
            f"branch {request.branch}, and name in every Work Order the worktree and branch git reports "
            f"at dispatch. Your plan {plan} holds only the "
            f"user's words. Write the full phased plan there, send it to the showrunner, and wait for its "
            f"approval before you run /unit:direct {plan}.")


def attach_hint(request: UnitLaunch) -> str:
    """Where the user finds the unit: its tab in the production's workspace of the shared session."""

    production = request.production
    tab = request.identity.session
    return herdr.attach_hint(production.name, tab)


def host_session_name(request: UnitLaunch) -> str:
    """Name the unit destination from its production and staff name."""

    production_name = request.production.name
    name = request.identity.session
    return herdr.generated_session_name(unit_names.branch(production_name, name))


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
    """Start Claude in a tab of the shared herdr session, carrying its session labels."""
    from scripts.accounts import accounts
    from scripts.shutdown.launch_names import LaunchNames
    from scripts.whoami.account import named_account_environment

    environment = {"ENABLE_TOOL_SEARCH": "true"}
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
        session_id = ""
    if autocompact_window:
        arguments.extend(["--autocompact", autocompact_window])
    saved_name = request.identity.session
    session_name = LaunchNames.read().name(session_id, saved_name)
    profile = request.profile
    flags = profile.flags if isinstance(profile, ProfiledLaunch) else ("--settings", '{"disableAgentView": true}')
    arguments.extend(["--remote-control", session_name, "-n", session_name, *flags])
    timeout_ms = int(request.timeout * 1000)
    pending = opening_record(request.production, request.unit_id)
    pending.parent.mkdir(parents=True, exist_ok=True)
    def published(transcript: herdr.SessionTranscript) -> None:
        write_text_atomic(pending, json.dumps({
            "session_id": transcript.session_id, "config_dir": str(transcript.config_dir),
            "prompt": prompt, "launched_at": datetime.now(UTC).isoformat(),
        }) + "\n")
        _ = ensure_staff_unit(request.production, request.unit_id,
            worktree=staff.PresentWorktree(remote.local_machine(), request.worktree.resolve()),
            runtime_id=transcript.session_id)
    host = herdr.shared_session()
    _ = herdr.start_server(host, timeout_ms)
    production = request.production
    launched = herdr.launch_agent(host, production.name,
        session_name, str(request.session_cwd), environment, kind="claude", agent=host_session_name(request),
        agent_arguments=arguments, opening=herdr.OpeningPrompt(prompt, herdr.SessionTranscript(selected.dir, session_id),
            published),
        timeout_ms=timeout_ms)
    if isinstance(launched, herdr.nix_env.Refused):
        raise Refusal(launched.reason)
    outcome = launched.outcome
    if isinstance(outcome, herdr.PromptSubmitted):
        pending.unlink(missing_ok=True)
    return outcome if isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted)) else herdr.PromptNotSubmitted("launcher returned no submit proof")


def opening_record(production: Production, unit: str) -> Path:
    """Keep pending prompt work in the checkout's Git directory, outside tracked files."""
    checkout = production.checkout
    directory = checkout / ".git"
    if directory.is_file():
        pointer = directory.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise Refusal(f"invalid Git directory pointer: {directory}")
        directory = (checkout / pointer.removeprefix("gitdir: ")).resolve()
    return directory / "add-unit-openings" / f"{unit}.json"


def pending_opening(production: Production, unit: str, session_id: str) -> bool:
    """Whether this unit still owes an opening to the named conversation."""
    path = opening_record(production, unit)
    if not path.exists():
        return False
    fields = cast(dict[str, str], json.loads(path.read_text(encoding="utf-8")))
    return fields["session_id"] == session_id


class NoPendingOpening(NamedTuple):
    """No live conversation still needs this saved opening."""


def finish_opening(request: UnitLaunch, marked: LabeledSessionFound) -> herdr.PromptSubmitOutcome | NoPendingOpening:
    """Finish the opening for this live session, without starting a second session."""

    pending = opening_record(request.production, request.unit_id)
    if not pending.exists():
        return NoPendingOpening()
    fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
    claude = marked.unit.claude
    if not isinstance(claude, unit_lookup.LiveClaude) or claude.session_id != fields["session_id"]:
        observed = sessions.session_liveness(fields["session_id"])
        if isinstance(observed, sessions.SessionNotLive):
            pending.unlink()
            return NoPendingOpening()
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise Refusal(f"cannot tell whether pending session {fields['session_id']} runs: {observed.reason}")
        raise Refusal("pending opening does not name the session that carries the unit's session label")
    transcript = herdr.SessionTranscript(Path(fields["config_dir"]), fields["session_id"])
    _ = ensure_staff_unit(request.production, request.unit_id,
        worktree=staff.PresentWorktree(remote.local_machine(), request.worktree.resolve()),
        runtime_id=transcript.session_id)
    at = datetime.fromisoformat(fields["launched_at"])
    outcome = herdr.submitted_prompt(transcript, fields["prompt"], at)
    if not isinstance(outcome, herdr.PromptSubmitted):
        outcome = herdr.Herdr(marked.unit.place.session).submit_opening_prompt(
            marked.unit.place.pane, herdr.OpeningPrompt(fields["prompt"], transcript), at, int(request.timeout * 1000))
    if isinstance(outcome, herdr.PromptSubmitted):
        pending.unlink(missing_ok=True)
    return outcome


def close_untyped_session(request: UnitLaunch, outcome: herdr.PromptNotSubmitted) -> str:
    """Close a new session that never got its prompt, so no half-started unit stays behind.

    The staff row, .unit.toml and worktree stay: the same command finds them and starts the session
    again. A prompt typed even once may have started work, so that session stays open.
    """

    if not outcome.never_typed:
        return ""
    found = launched_unit(request)
    if isinstance(found, NoLabeledSession):
        return "; run the same command again"
    try:
        herdr.close_agent(found.unit.place.session, found.unit.place.pane)
    except herdr.HerdrFailure as error:
        return f"; its session {found.unit.place.session} could not be closed: {error}"
    return "; its session is closed: run the same command again"


class NoOpeningRecord(NamedTuple):
    """This unit has no saved opening to reconcile."""


class DeadPendingOpening(NamedTuple):
    """The saved opening's native session is confirmed stopped."""

    record: Path


class LivePendingOpening(NamedTuple):
    """The saved opening's native session has a running process."""

    session_id: str
    process: sessions.LiveSessionProcess


def _pending_launch(request: UnitLaunch | RequestedUnitLaunch) -> NoOpeningRecord | DeadPendingOpening | LivePendingOpening:
    """Observe pending launch liveness without changing its recovery evidence."""
    name = request.unit if isinstance(request, RequestedUnitLaunch) else request.identity.unit
    pending = opening_record(request.production, name)
    if not pending.exists():
        return NoOpeningRecord()
    fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
    native = str(staff.native_id(fields["session_id"]))
    live = sessions.session_liveness(native)
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise Refusal(f"cannot locate pending session {native}: {live.reason}")
    if isinstance(live, sessions.LiveSessionProcess):
        return LivePendingOpening(native, live)
    return DeadPendingOpening(pending)


def launched_unit(request: UnitLaunch | RequestedUnitLaunch) -> LabeledSessionFound | NoLabeledSession:
    """Find the unit session through its pending opening or native runtime identity."""

    name = request.unit if isinstance(request, RequestedUnitLaunch) else request.identity.unit
    pending = _pending_launch(request)
    if isinstance(pending, LivePendingOpening):
        native = pending.session_id
        place = herdr.herdr_place_of(pending.process.pid)
        if isinstance(place, herdr.NotInHerdr):
            raise Refusal(f"pending session {native} runs outside herdr")
        observed = herdr.Herdr(place.session).native_session(place.pane, "claude", request.timeout)
        if observed != native:
            raise Refusal(f"pending session {native} differs from pane identity {observed}")
        return LabeledSessionFound(unit_lookup.LabeledSession(name,
            unit_lookup.LiveClaude(name, native, "", pending.process.pid), place, identity=unit_lookup.LiveSession(native)))
    unit = unit_lookup.labeled_session(request.production.name, name)
    return NoLabeledSession() if unit is None else LabeledSessionFound(unit)


def registry_remote_control(claude: unit_lookup.Claude) -> bool:
    """Remote Control is on when this session's registry record carries a bridge session.

    The record does not scroll off, as the pane's "/remote-control is active" line does
    once the opening prompt starts work.
    """
    from scripts.message import sessions

    if not isinstance(claude, unit_lookup.LiveClaude):
        return False
    record = sessions.read_session(sessions.sessions_dir() / f"{claude.pid}.json")
    if isinstance(record, sessions.UnreadableSessionRecord) or record["sessionId"] != claude.session_id:
        return False
    return sessions.remote_control(record) == "active"


def wait_for_remote_control(request: UnitLaunch) -> unit_lookup.LabeledSession:
    found = launched_unit(request)
    if isinstance(found, NoLabeledSession):
        raise RuntimeError(f"{request.identity.unit}: no session has its native runtime identity")
    launched = found.unit
    deadline = time.monotonic() + request.timeout
    while True:
        if not isinstance(launched.claude, unit_lookup.LiveClaude):
            # Read the session again until its native identity has a live Claude.
            try:
                again = launched_unit(request)
            except OSError:
                again = found
            if isinstance(again, LabeledSessionFound):
                launched = again.unit
        if registry_remote_control(launched.claude):
            return launched
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"anomaly: {request.identity.unit}: registry has no remote-control evidence", file=sys.stderr)
            raise RuntimeError(f"{request.identity.unit} (herdr {launched.place.session}:{launched.place.pane}): /remote-control did not become active")
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
    """Resume a saved conversation on its launch account; otherwise use the units account."""
    from scripts.accounts import accounts
    try:
        if isinstance(request.session, ResumedSession) and not config_dir:
            from scripts.production import unit_move

            conversation = unit_move.unit_conversation(None, session_id=request.session.session_id)
            if source_fallback and (not isinstance(conversation, unit_move.UnitConversation)
                                    or conversation.account != target):
                raise Refusal("--source-fallback must name the pane launch's source account")
            if isinstance(conversation, unit_move.UnitConversation) and (not target or target == conversation.account):
                prepared = accounts.prepared_account(conversation.account, resume=True)
                if isinstance(prepared, accounts.AccountRefused):
                    raise Refusal(prepared.reason)
                return accounts.NamedAccount(conversation.account, accounts.claude_account_dir(conversation.account))
        elif source_fallback:
            raise Refusal("--source-fallback requires a resumed session and its pane launch account")
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
    """Use the pane launch model when available, with --model as the restore boundary."""
    if isinstance(request.session, NewSession):
        if explicit:
            raise Refusal("--model requires --resume")
        return ""
    if explicit:
        return explicit
    from scripts.shutdown import pane_record
    try:
        entry = pane_record.launch_for_session(request.session.session_id)
    except (OSError, ValueError) as error:
        raise Refusal(f"pane launch unreadable: {error}") from error
    if entry is not None and entry["model"]:
        return entry["model"]
    return transcript_model(request.session.session_id) if isinstance(launched_unit(request), LabeledSessionFound) else ""


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


def registered_unit(request: RequestedUnitLaunch, record: staff.StaffSnapshot) -> bool:
    """Require complete admission before a resume can change only its settings."""
    if not isinstance(request.session, ResumedSession):
        return False
    found = checked_unit_member(request.production, request.unit, record,
                                identity=session_staff.LiveSession(request.session.session_id))
    if not isinstance(found, staff_sources.MemberFound) or not isinstance(found.row.worktree, staff.PresentWorktree):
        return False
    worktree = unit_lookup.local_worktree(found).path
    return isinstance(unit_config.read(worktree, cleanup=False), unit_config.UnitConfig)


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


def configuration_for(request: UnitLaunch) -> unit_config.UnitConfig:
    """Keep saved responsibilities or use explicit launch responsibilities."""
    existing = unit_config.read(request.worktree, cleanup=False)
    if isinstance(request.session, ResumedSession) and isinstance(existing, unit_config.UnitConfig):
        return existing
    plan = (unit_config.PlanFile(request.plan_path) if not isinstance(request.plan, Standby) else
            unit_config.PermanentPlan() if request.plan.permanent else unit_config.StandbyPlan())
    try:
        port = (unit_config.parse_port_cell(request.port.value) if isinstance(request.port, SuppliedCell)
                else existing.port if isinstance(existing, unit_config.UnitConfig) else unit_config.NoAppPort())
    except unit_config.ConfigRefused as error:
        raise Refusal(f"unit {request.unit_id}: {error}") from error
    responsibilities = (existing.responsibilities if isinstance(existing, unit_config.UnitConfig) else
                        tuple(dict.fromkeys(request.responsibilities)))
    config = unit_config.UnitConfig(plan, port, responsibilities)
    if isinstance(existing, unit_config.UnitConfig):
        comparable = existing
        stub = stub_plan(request.production, request.branch)
        if (isinstance(request.plan, PlanGiven) and existing.plan == unit_config.PlanFile(stub)
                and recorded_mode(request, stub.as_posix()) == "brief"):
            comparable = replace(existing, plan=plan)
        key = unit_config.first_difference(comparable, config)
        if key:
            raise Refusal(f"{request.worktree / '.unit.toml'}: {key} differs; no file was changed")
    elif (request.worktree / ".unit.toml").exists() or (request.worktree / ".unit.toml").is_symlink():
        raise Refusal(existing.reason)
    return config


@dataclass(frozen=True)
class UnitAdmissionPending:
    """Repository admission is ready; the backend has not published native membership."""


@dataclass(frozen=True)
class UnitMembershipPublished:
    """An observed native backend has durable unit membership."""

    row: staff.StaffRow


def admit_unit(request: UnitLaunch, *,
               record: staff.StaffSnapshot | None = None) -> UnitAdmissionPending | UnitMembershipPublished:
    """Create the actual worktree before the row, then publish the checked config."""
    record = staff.initial_snapshot() if record is None else record
    config = configuration_for(request)
    ensure_worktree(request)
    location = staff.PresentWorktree(remote.local_machine(), request.worktree.resolve())
    git_exclude.require_untracked_ignored(request.worktree, ".unit.toml", planned_pattern="/.unit.toml")
    selected = checked_unit_member(request.production, request.unit_id, record,
                                   identity=(session_staff.LiveSession(request.session.session_id)
                                             if isinstance(request.session, ResumedSession) else session_staff.NO_SESSION))
    row = selected.row if isinstance(selected, staff_sources.MemberFound) else None
    if row is not None:
        row = ensure_staff_unit(request.production, request.unit_id, worktree=location, record=record,
                                runtime_id=request.session.session_id if isinstance(request.session, ResumedSession) else "")
    _ = unit_config.write(request.worktree, config)
    return UnitMembershipPublished(row) if row is not None else UnitAdmissionPending()


def publish_unit(ready: ReadyToLaunch, *,
                 record: staff.StaffSnapshot | None = None) -> UnitAdmissionPending | UnitMembershipPublished:
    """Admit the actual worktree before committing its stub plan."""
    request = ready.launch
    before = git(request.production, "rev-parse", request.production.merge_branch).stdout.strip()
    pending = opening_record(request.production, request.unit_id).parent.parent / "add-unit-admissions" / f"{request.unit_id}.base"
    publication = admit_unit(request, record=record)
    if (not pending.exists()
            and (isinstance(ready.row, NoUnitRow) or isinstance(ready.plan_transition, BriefToPlanTransition))
            and git(request.production, "-C", str(request.worktree), "rev-parse", "HEAD").stdout.strip() == before):
        write_text_atomic(pending, before + "\n")
    write_stub(request)
    commit_unit(request, ready.plan_transition)
    # Keep the original base until the worktree includes its admission commit.
    if pending.exists():
        base = pending.read_text(encoding="utf-8").strip()
        target = git(request.production, "rev-parse", request.production.merge_branch).stdout.strip()
        head = git(request.production, "-C", str(request.worktree), "rev-parse", "HEAD").stdout.strip()
        if head == base:
            _ = git(request.production, "-C", str(request.worktree), "merge", "--ff-only", target)
        elif git(request.production, "merge-base", "--is-ancestor", target, head, check=False).returncode:
            raise Refusal(f"unit {request.unit_id}: unfinished admission worktree moved from {base}; no launch was started")
        pending.unlink()
    return publication


def checked_unit_member(production: Production, unit: str,
                        record: staff.StaffSnapshot, *,
                        identity: session_staff.LiveSession | session_staff.NoSession = session_staff.NO_SESSION,
                        ) -> staff_sources.MemberFound | staff_sources.NoSuchMember:
    """Validate existing ownership before admission makes any repository change."""
    selector = staff_sources.UnitMember(production.name, unit)
    if isinstance(identity, session_staff.LiveSession):
        found = staff_sources.member(staff_sources.ObservedUnit(unit, identity), record)
    else:
        # A new admission checks reserved names in staff, without observing a process.
        reservations = [row.id for row in record.find(kind="unit") if row.name == unit_names.name(unit)]
        if len(reservations) > 1:
            raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
        row = record.by_id(reservations[0]) if reservations else None
        found = staff_sources.MemberFound(row) if row is not None else staff_sources.NoSuchMember(selector)
    if isinstance(found, staff_sources.NoSuchMember) and isinstance(identity, session_staff.LiveSession):
        if any(row.name == unit_names.name(unit) for row in record.find(kind="unit")):
            raise Refusal(f"{unit}: native session conflicts with retained staff identity")
    if isinstance(found, staff_sources.MemberFound):
        if isinstance(found.row.worktree, staff.RemovedWorktree):
            raise Refusal(f"unit {unit} is retired; resume that unit, or remove it for good to free the name")
        if found.row.parent != staff.ReportsTo(production.owner.id):
            owner = record.by_id(found.row.parent.id) if isinstance(found.row.parent, staff.ReportsTo) else None
            if owner is None or owner.kind != "showrunner":
                raise staff.StaffUnreadable(f"{found.row.name}: no showrunner staff row")
            raise Refusal(f"{found.row.name} is under {owner.name}; to move it, run: "
                          + f"staff.py move {owner.name}/{found.row.name} {production.name}")
    return found


def ensure_staff_unit(production: Production, unit: str, *,
                      worktree: staff.PresentWorktree | staff.WorktreeUnfilled = staff.WORKTREE_UNFILLED,
                      runtime_id: str = "",
                      record: staff.StaffSnapshot | None = None) -> staff.StaffRow:
    """Admit a unit with its location; only agents resume --retired reactivates a removed row."""
    record = staff.initial_snapshot() if record is None else record
    found = checked_unit_member(production, unit, record,
                                identity=session_staff.LiveSession(runtime_id) if runtime_id else session_staff.NO_SESSION)
    if isinstance(found, staff_sources.MemberFound):
        if runtime_id and found.row.current_session != staff.native_id(runtime_id):
            raise Refusal(f"{unit}: native session conflicts with retained staff identity")
        if isinstance(worktree, staff.WorktreeUnfilled) or worktree == found.row.worktree:
            return found.row
        return staff.set_worktree(found.row.id, worktree, expected=found.row.worktree)
    if isinstance(worktree, staff.WorktreeUnfilled):
        raise Refusal(f"unit {unit}: no filled worktree for admission")
    runner = ensure_staff_showrunner(production)
    return staff.ensure("unit", name=unit_names.name(unit), parent=staff.ReportsTo(runner.id), worktree=worktree, runtime_id=runtime_id)

def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("name")
    _ = parser.add_argument("--plan")
    _ = parser.add_argument("--brief")
    _ = parser.add_argument("--standby", action="store_true")
    _ = parser.add_argument("--permanent", action="store_true")
    _ = parser.add_argument("--port", type=int)
    _ = parser.add_argument("--responsibility", action="append", default=[], metavar="NAME")
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
    _ = parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = launch_request(args)
        with staff.row_operation(request.session.session_id if isinstance(request.session, ResumedSession)
                                 else request.unit_id):
            identity_record = staff.initial_snapshot()
            request = launch_request(args, record=identity_record)
            explicit_autocompact = cast(str, args.autocompact)
            if (explicit_autocompact and isinstance(request.session, ResumedSession)
                    and isinstance(request.session.handoff, ResumeStoppedSession)
                    and registered_unit(request, identity_record)
                    and not pending_opening(request.production, request.unit, request.session.session_id)):
                # Setting a registered unit's window is not new work: no new-work check or launch applies.
                set_autocompact(request, request.session.session_id, explicit_autocompact, cast(bool, args.check))
                return 0
            # Readers import this module from copied trees without accounts.
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

            handoff: NoSessionHandoff | StoppedSessionHandoff = NoSessionHandoff()
            if isinstance(request.session, ResumedSession) and isinstance(request.session.handoff, AttachRunningSession):
                attach_ready = preflight(request, unit_account, record=identity_record)
                source = attach_ready.handoff
                if cast(bool, args.check):
                    return 0
                assert isinstance(source, RunningSessionHandoff)
                stop_for_attach(source)
                require_stopped_session(source)
                handoff = StoppedSessionHandoff(source)
                request = replace(request, session=request.session._replace(handoff=ResumeStoppedSession()))

            outcome = herdr.PromptNotSubmitted("restore reused a session without submit proof")
            ready = preflight(request, unit_account, record=identity_record)
            request = ready.launch
            if cast(bool, args.check):
                return 0
            pending = _pending_launch(request)
            if isinstance(pending, DeadPendingOpening):
                pending.record.unlink()
            labeled_session = launched_unit(request)
            stopped_tab: LabeledSessionFound | NoLabeledSession = NoLabeledSession()
            if (
                isinstance(request.session, ResumedSession)
                and isinstance(labeled_session, LabeledSessionFound)
            ):
                unit = labeled_session.unit
                if isinstance(unit.claude, unit_lookup.ClaudeUnknown):
                    raise Refusal(
                        f"cannot tell whether Claude runs in session {unit.place.session}: "
                        + unit.claude.reason
                    )
                if isinstance(unit.claude, unit_lookup.ClaudeNotRunning):
                    stopped_tab = labeled_session
                    labeled_session = NoLabeledSession()
            if isinstance(handoff, StoppedSessionHandoff) and isinstance(labeled_session, LabeledSessionFound):
                raise Refusal(f"cannot attach {handoff.session_id}: unit tab already runs")
            if isinstance(labeled_session, LabeledSessionFound):
                retry = shlex.join(["add_unit.py", *argv])
                if isinstance(ready.row, NoUnitRow):
                    raise Refusal(f"unit {request.identity.unit} already runs in session {labeled_session.unit.place.session} "
                                  + f"but has no .unit.toml; end that session, then run {retry}")
                if isinstance(ready.plan_transition, BriefToPlanTransition):
                    raise Refusal(f"unit {request.identity.unit} runs its brief in session {labeled_session.unit.place.session}; "
                                  + f"end that session, then run {retry}")
            _ = publish_unit(ready, record=identity_record)
            if isinstance(stopped_tab, LabeledSessionFound):
                place = stopped_tab.unit.place
                try:
                    herdr.close_agent(place.session, place.pane)
                except herdr.HerdrFailure as error:
                    raise Refusal(f"cannot end session {place.session}: {error}") from error
            if cast(str, args.autocompact) and isinstance(request.session, ResumedSession):
                autocompact.record(request.session.session_id, cast(str, args.autocompact))
            if isinstance(labeled_session, NoLabeledSession):
                prompt = prompt_for(request)
                outcome = launch_session(request, ready.director, prompt, unit_account, target=unit_account.label
                               if isinstance(request.session, ResumedSession) else "", model=resume_model,
                               autocompact_window=resume_autocompact)
            elif opening_record(request.production, request.unit_id).exists():
                recovered = finish_opening(request, labeled_session)
                if not isinstance(recovered, NoPendingOpening):
                    outcome = recovered
                if isinstance(recovered, herdr.PromptNotSubmitted):
                    raise Refusal(f"{request.identity.unit}: opening prompt not submitted: {recovered.reason}")
            if isinstance(outcome, herdr.ClaudeTrustNotWritten):
                return 1
            if isinstance(outcome, herdr.PromptNotSubmitted) and isinstance(labeled_session, NoLabeledSession):
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
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except (Refusal, unit_config.ConfigRefused, git_exclude.ExcludeRefused) as error:
        print(f"add_unit: {error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, subprocess.SubprocessError, RuntimeError, ValueError, herdr.HerdrFailure) as error:
        detail = str(error)
        print(f"add_unit: {detail}", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
