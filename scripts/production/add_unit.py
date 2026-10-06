#!/usr/bin/env python3
"""Add one unit to a running production, with restartable launch steps."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo


class Production(NamedTuple):
    doc: Path
    slug: str
    merge_branch: str
    checkout: Path
    showrunner_session: str
    log: Path
    zone: ZoneInfo


class PlanGiven(NamedTuple):
    path: Path


class BriefGiven(NamedTuple):
    words: str
    stub: Path


class NewSession(NamedTuple):
    pass


class ResumedSession(NamedTuple):
    session_id: str
    cwd: Path


class UnitLaunch(NamedTuple):
    production: Production
    name: str
    unit: str
    branch: str
    worktree: Path
    plan: PlanGiven | BriefGiven
    port: str
    owns: str
    session: NewSession | ResumedSession
    timeout: float

    @property
    def plan_path(self) -> Path:
        return self.plan.path if isinstance(self.plan, PlanGiven) else self.plan.stub

    @property
    def mode_name(self) -> str:
        return "plan" if isinstance(self.plan, PlanGiven) else "brief"


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without interpreting its commentary."""
    prefix = f"- **{field}:** "
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix):]
            quoted = re.search(r"`([^`]+)`", value)
            result = quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()
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
        showrunner_session = production_field(lines, "Showrunner session")
        log = Path(production_field(lines, "Log"))
        zone = ZoneInfo(production_field(lines, "User zone"))
        slug = doc.name.removesuffix("-production.md")
        if slug == doc.name:
            raise Refusal("production doc name must end in -production.md")
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        if "## Units" not in lines:
            raise Refusal("production doc lacks Units table")
        return Production(doc, slug, merge_branch, checkout, showrunner_session, checkout / log, zone)
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


def launch_request(args: argparse.Namespace) -> UnitLaunch:
    production = read_production(Path(cast(str, args.production)))
    name = cast(str, args.name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise Refusal("name must contain only letters, numbers, hyphens, or underscores")
    given_plan = cast(str | None, args.plan)
    given_brief = cast(str | None, args.brief)
    if (given_plan is None) == (given_brief is None):
        raise Refusal("give exactly one of --plan or --brief")
    kebab = name.replace("_", "-")
    branch = f"{production.merge_branch}-{kebab}"
    checkout_name = production.checkout.name.removesuffix("-trunk")
    worktree = production.checkout.parent / f"{checkout_name}-{kebab}"
    plan: PlanGiven | BriefGiven
    if given_plan is not None:
        plan = PlanGiven(relative_plan(production, given_plan))
    else:
        assert given_brief is not None
        plan = BriefGiven(given_brief, Path("docs/plans") / f"{branch}.md")
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
    return UnitLaunch(production, name, f"{name}-unit", branch, worktree, plan,
                      str(port) if port is not None else "—", cast(str | None, args.owns) or "—",
                      session, timeout)


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


def desired_row(request: UnitLaunch) -> str:
    return (f"| {request.unit} | {request.plan_path.as_posix()} | {request.worktree} | "
            f"{request.branch} | {request.name} | {request.port} | {request.owns} |")


def row_is_present(request: UnitLaunch, lines: list[str]) -> bool:
    _, rows = unit_rows(lines)
    for row in rows:
        fields = [field.strip() for field in row.strip("|").split("|")]
        if len(fields) >= 4 and fields[0] != request.unit and (
                fields[2] == str(request.worktree) or fields[3] == request.branch):
            raise Refusal(f"branch or worktree for {request.name} is already used in Units table")
    matches = [row for row in rows if row.split("|", 2)[1].strip() == request.unit]
    if matches and matches != [desired_row(request)]:
        raise Refusal(f"unit name {request.name} is already taken in Units table")
    return bool(matches)


def recorded_mode(request: UnitLaunch) -> str:
    subject_prefix = f"production({request.production.slug}): add unit {request.unit} ("
    subjects = git(request.production, "log", "--format=%s", "--", str(
        request.production.doc.relative_to(request.production.checkout))).stdout.splitlines()
    for subject in subjects:
        if subject in (subject_prefix + "plan)", subject_prefix + "brief)"):
            return subject.removeprefix(subject_prefix).removesuffix(")")
    stub = request.production.checkout / "docs/plans" / f"{request.branch}.md"
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


def tmux_binary() -> str:
    found = shutil.which("tmux")
    if found:
        return found
    result = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", "nixpkgs#tmux^out"],
                            text=True, capture_output=True, check=True)
    return str(Path(result.stdout.strip()) / "bin/tmux")


def tmux_live(tmux: str, name: str) -> bool:
    result = subprocess.run([tmux, "has-session", "-t", f"={name}"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def preflight(request: UnitLaunch) -> bool:
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
    if existing and recorded_mode(request) != request.mode_name:
        raise Refusal(f"unit name {request.name} is already taken in Units table")
    has_worktree = worktree_on_branch(request)
    if has_worktree and not existing:
        raise Refusal(f"worktree path {request.worktree} is already in use")
    local_branch = git(production, "show-ref", "--verify", "--quiet",
                       f"refs/heads/{request.branch}", check=False).returncode == 0
    if (local_branch or remote_head(production, request.branch)) and not (existing and has_worktree):
        raise Refusal(f"branch {request.branch} already exists")
    if isinstance(request.plan, PlanGiven):
        result = git(production, "show", f"{production.merge_branch}:{request.plan.path.as_posix()}", check=False)
        if result.returncode != 0 or not any(line.startswith("> **Production:") for line in result.stdout.splitlines()):
            raise Refusal(f"plan {request.plan.path} is missing on {production.merge_branch} or lacks the Production header")
    elif (production.checkout / request.plan.stub).exists():
        stub = production.checkout / request.plan.stub
        content = stub.read_text(encoding="utf-8")
        source = content.split("## Source\n\n", 1)
        saved_words = source[1].split("\n\n", 1) if len(source) == 2 else []
        if (not any(line.startswith("> **Production:") for line in content.splitlines())
                or len(saved_words) != 2 or saved_words[1] != request.plan.words + "\n"):
            raise Refusal(f"stub plan {request.plan.stub} already exists with different content")
    return existing


def write_stub(request: UnitLaunch) -> None:
    if not isinstance(request.plan, BriefGiven):
        return
    path = request.production.checkout / request.plan.stub
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = request.production.doc.relative_to(request.production.checkout).as_posix()
    stamp = datetime.now(request.production.zone).strftime("%Y-%m-%d %H:%M %Z")
    header = f"# {request.name}\n\n> **Production: {request.production.slug}** — unit "
    source = f"`{request.unit}`; production doc `{doc}`\n\n## Source\n\n{stamp}\n\n{request.plan.words}\n"
    _ = path.write_text(header + source, encoding="utf-8")


def append_row(request: UnitLaunch, already_present: bool) -> None:
    if already_present:
        return
    doc = request.production.doc
    lines = doc.read_text(encoding="utf-8").splitlines()
    insert_at, _ = unit_rows(lines)
    lines.insert(insert_at, desired_row(request))
    _ = doc.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remote_head(production: Production, branch: str) -> str:
    result = git(production, "ls-remote", "--heads", "origin", branch)
    return result.stdout.split()[0] if result.stdout.strip() else ""


def commit_and_push(request: UnitLaunch) -> None:
    production = request.production
    paths = [str(production.doc.relative_to(production.checkout))]
    if isinstance(request.plan, BriefGiven):
        paths.append(str(request.plan.stub))
    status = git(production, "status", "--porcelain", "--", *paths).stdout
    if status:
        _ = git(production, "add", "--", *paths)
        _ = git(production, "commit", "--only", "-m",
                f"production({production.slug}): add unit {request.unit} ({request.mode_name})", "--", *paths)
    head = git(production, "rev-parse", production.merge_branch).stdout.strip()
    if remote_head(production, production.merge_branch) != head:
        _ = git(production, "push", "origin", production.merge_branch)


def ensure_worktree(request: UnitLaunch) -> None:
    production = request.production
    worktree = request.worktree
    if worktree.exists():
        _ = worktree_on_branch(request)
    else:
        _ = git(production, "worktree", "add", str(worktree), "-b", request.branch,
                production.merge_branch)
    head = git(production, "rev-parse", request.branch).stdout.strip()
    upstream = git(production, "config", "--get", f"branch.{request.branch}.remote", check=False).stdout.strip()
    if remote_head(production, request.branch) != head or upstream != "origin":
        _ = git(production, "push", "-u", "origin", request.branch)
    if (production.checkout / ".claude/config/berth.toml").exists():
        key = f"branch.{request.branch}.cargoBerthTarget"
        current = git(production, "config", "--get", key, check=False).stdout.strip()
        if current != production.merge_branch:
            _ = git(production, "config", key, production.merge_branch)


def prompt_for(request: UnitLaunch) -> str:
    production = request.production
    doc = str(production.doc)
    plan = request.plan_path.as_posix()
    if isinstance(request.session, ResumedSession):
        plan = str(request.worktree / request.plan_path)
        return (f"You are now {request.unit} in production {production.slug} (doc {doc}), under the "
                f"showrunner {production.showrunner_session}. Work only in your worktree {request.worktree}, "
                f"branch {request.branch}, and name it in every Work Order. Run /unit:delegate {plan}.")
    if isinstance(request.plan, PlanGiven):
        return f"/unit:delegate {plan}"
    return (f"You are {request.unit} in production {production.slug} (doc {doc}), under the "
            f"showrunner {production.showrunner_session}. Work only in your worktree {request.worktree}, "
            f"branch {request.branch}, and name it in every Work Order. Your plan {plan} holds only the "
            f"user's words. Write the full phased plan there, send it to the showrunner, and wait for its "
            f"approval before you run /unit:delegate {plan}.")


def launch_session(request: UnitLaunch, tmux: str) -> None:
    if tmux_live(tmux, request.name):
        return
    argv = ["claude"]
    if isinstance(request.session, ResumedSession):
        argv.extend(["--resume", request.session.session_id])
    argv.extend(["--remote-control", request.name, "-n", request.name,
                 "--settings", '{"disableAgentView": true}', prompt_for(request)])
    command = "ENABLE_TOOL_SEARCH=true command " + shlex.join(argv) + "; exec zsh"
    cwd = request.session.cwd if isinstance(request.session, ResumedSession) else request.worktree
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}
    _ = subprocess.run(["systemd-run", "--user", "--scope", f"--unit={request.name}", tmux,
                        "new-session", "-d", "-s", request.name, "-c", str(cwd),
                        "-e", f"SHOWRUNNER_UNIT={request.production.slug}", "zsh", "-ic", command],
                       env=environment, text=True, capture_output=True, check=True)


def wait_for_remote_control(request: UnitLaunch, tmux: str) -> None:
    deadline = time.monotonic() + request.timeout
    pane = ""
    while True:
        result = subprocess.run([tmux, "capture-pane", "-p", "-t", f"={request.name}:"],
                                text=True, capture_output=True, check=False)
        pane = result.stdout if result.returncode == 0 else result.stderr
        if "/remote-control is active" in pane:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = "\n".join(pane.splitlines()[-15:])
            raise RuntimeError(f"{request.name}: /remote-control did not become active\n{tail}")
        time.sleep(min(0.5, remaining))


def update_old_prompt(request: UnitLaunch) -> None:
    path = Path.home() / ".local/state/showrunner" / request.production.slug / "prompt.txt"
    if not path.exists():
        return
    prompt = path.read_text(encoding="utf-8")
    pattern = re.compile(r"unit_status\.sh\s+\S+\s+\S+(?P<units>[^|`\n]*)")
    match = pattern.search(prompt)
    if match is None or "--showrunner" in match.group(0):
        return
    units = shlex.split(match.group("units").strip())
    if request.name in units:
        return
    replacement = match.group(0).rstrip() + " " + shlex.quote(request.name) + match.group(0)[len(match.group(0).rstrip()):]
    changed = prompt[:match.start()] + replacement + prompt[match.end():]
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".prompt-", delete=False,
                                     encoding="utf-8") as temporary:
        _ = temporary.write(changed)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def registry_has_unit(request: UnitLaunch) -> bool:
    path = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
    if not path.exists():
        return False
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        return False
    runners = cast(dict[str, object], raw).get("showrunners")
    if not isinstance(runners, list):
        return False
    for item in cast(list[object], runners):
        if not isinstance(item, dict):
            continue
        runner = cast(dict[str, object], item)
        units = runner.get("units")
        if (runner.get("session") == request.production.showrunner_session
                and runner.get("zone") == request.production.zone.key
                and isinstance(units, list) and request.name in cast(list[object], units)):
            return True
    return False


def record(request: UnitLaunch) -> None:
    production = request.production
    if not registry_has_unit(request):
        script = Path(__file__).resolve().parent / "showrunners.py"
        _ = subprocess.run([sys.executable, str(script), "add", production.showrunner_session,
                            "--zone", production.zone.key, "--unit", request.name],
                           text=True, capture_output=True, check=True)
    update_old_prompt(request)
    log_line = (f"added {request.unit} ({request.mode_name}), tmux {request.name}, "
                f"worktree {request.worktree}")
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
    _ = parser.add_argument("--port", type=int)
    _ = parser.add_argument("--owns")
    _ = parser.add_argument("--timeout", type=float, default=90.0)
    _ = parser.add_argument("--resume")
    _ = parser.add_argument("--cwd")
    args = parser.parse_args(argv)
    try:
        request = launch_request(args)
        existing = preflight(request)
        tmux = tmux_binary()
        if tmux_live(tmux, request.name) and not existing:
            raise Refusal(f"tmux session {request.name} is already live")
        write_stub(request)
        append_row(request, existing)
        commit_and_push(request)
        ensure_worktree(request)
        launch_session(request, tmux)
        wait_for_remote_control(request, tmux)
        record(request)
        print(f"{request.unit} started: tmux attach -t {request.name}")
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
