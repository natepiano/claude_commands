#!/usr/bin/env python3
"""List, attach, start or resume standalone Claude sessions, each a tab of the shared herdr session.

A standalone session opens in the shared session's `ad-hoc` workspace; nothing here starts a herdr
session of its own (user, 2026-10-26).
"""

from __future__ import annotations

import argparse
import itertools
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import herdr

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "message"))
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session, sessions_dir  # noqa: E402

sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "production"))
from scripts.production.showrunners import registered_showrunners  # noqa: E402

from scripts.shutdown import launch_permission  # noqa: E402
from scripts.accounts import accounts  # noqa: E402
from scripts.whoami import account  # noqa: E402

USAGE = "usage: herdr_sessions.py list [--on <host>] | sort-tabs | attach <name> | focus <name> | attach-remote <host> <name> | start [--name N] [--cwd D] [-- <claude args>] | resume <name> --session-id ID --cwd D [--config-dir D] [--restore] [--prompt-file P] [--attach] [-- <claude args>]"
REMOTE_LIST_TIMEOUT = 15
ADHOC_SPACE = "ad-hoc"


@dataclass(frozen=True)
class RemoteHost:
    target: str

    def __post_init__(self) -> None:
        if not self.target or self.target.startswith("-") or any(character.isspace() for character in self.target):
            raise ValueError("remote host must be an SSH target, such as mac or user@mac")


def _remote_list(host: RemoteHost) -> int:
    try:
        result = subprocess.run(
            ["ssh", host.target, "~/.claude/scripts/lib/py", "~/.claude/scripts/herdr/herdr_sessions.py", "list"],
            capture_output=True, text=True, check=False, timeout=REMOTE_LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"herdr_sessions: {host.target}: remote list timed out after {REMOTE_LIST_TIMEOUT} seconds", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"herdr_sessions: {host.target}: remote list failed: {' '.join(str(error).split())}", file=sys.stderr)
        return 1
    if result.returncode:
        reason = " ".join(result.stderr.split()) or "no error message"
        print(f"herdr_sessions: {host.target}: remote list failed (ssh exit {result.returncode}): {reason}", file=sys.stderr)
        return 1
    print(result.stdout, end="")
    return 0


def _attach_remote(host: RemoteHost, name: str) -> int:
    """Show tab `name` on the host, then attach to the host's shared session."""
    result = subprocess.run(
        ["ssh", host.target, "~/.claude/scripts/lib/py", "~/.claude/scripts/herdr/herdr_sessions.py", "focus", name],
        capture_output=True, text=True, check=False, timeout=REMOTE_LIST_TIMEOUT,
    )
    if result.returncode:
        reason = " ".join(result.stderr.split()) or "no error message"
        print(f"herdr_sessions: {host.target}: {reason}", file=sys.stderr)
        return 1
    binary = herdr.herdr_binary()
    os.execvp(binary, [binary, "--remote", host.target, "--session", result.stdout.strip()])


class LaunchOptions(argparse.Namespace):
    name: str | None = None
    cwd: str = ""
    session_id: str = ""
    config_dir: str = ""
    restore: bool = False
    prompt_file: str = ""
    attach: bool = False


def _tab_pane(name: str) -> str:
    """The pane of the shared session's tab named `name`; empty when no such tab is open."""
    return next((pane["pane_id"] for pane in herdr.Herdr(herdr.shared_session()).panes() if pane.get("label") == name), "")


def _attach(pane: str) -> None:
    shared = herdr.shared_session()
    herdr.Herdr(shared).focus_tab_of(pane)
    binary = herdr.herdr_binary()
    os.execvp(binary, [binary, "session", "attach", shared])


def _launch_rerun(verb: str, options: LaunchOptions, folder: Path, claude_arguments: list[str]) -> str:
    command = [sys.executable, str(Path(__file__).resolve()), verb]
    if options.name is not None:
        command.extend(["--name", options.name] if verb == "start" else [options.name])
    command.extend(["--cwd", str(folder)])
    if verb == "resume":
        command.extend(["--session-id", options.session_id])
        if options.config_dir:
            command.extend(["--config-dir", str(Path(options.config_dir).expanduser().resolve())])
        if options.restore:
            command.append("--restore")
        if options.prompt_file:
            command.extend(["--prompt-file", str(Path(options.prompt_file).resolve())])
        if options.attach:
            command.append("--attach")
    if claude_arguments:
        command.extend(["--", *claude_arguments])
    return shlex.join(command)


def _launch(verb: str, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"herdr_sessions.py {verb}")
    if verb == "start":
        _ = parser.add_argument("--name")
        _ = parser.add_argument("--cwd", default=os.getcwd())
    else:
        _ = parser.add_argument("name")
        _ = parser.add_argument("--session-id", required=True)
        _ = parser.add_argument("--cwd", required=True)
        _ = parser.add_argument("--config-dir", default="")
        _ = parser.add_argument("--restore", action="store_true")
        _ = parser.add_argument("--prompt-file", default="")
        _ = parser.add_argument("--attach", action="store_true")
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    options = parser.parse_args(arguments[:separator], namespace=LaunchOptions())
    claude_arguments = arguments[separator + 1:]
    session_kind = herdr.security_session()
    if not isinstance(session_kind, (herdr.DesktopSession, herdr.NotMacSession)):
        print(herdr.MAC_DESKTOP_TERMINAL_REQUIRED)
        return 1
    folder = Path(options.cwd).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise ValueError(f"{folder}: not a directory")
    if options.name is not None:
        _ = herdr.server_unit(options.name)
    selected = accounts.account_for_unscoped_launch(
        dict(os.environ, CLAUDE_CONFIG_DIR=options.config_dir) if options.config_dir else os.environ)
    unset_variables = {"CLAUDE_CODE_OAUTH_TOKEN"}
    if isinstance(selected, accounts.LegacyHomeLaunch):
        config_dir = accounts.main_config_dir()
        pane_environment: dict[str, str] = {}
        unset_variables.update(account.NAMED_ACCOUNT_VARIABLES)
        claude_json = Path.home() / ".claude.json"
    else:
        config_dir = selected.dir
        pane_environment = account.named_account_environment(config_dir)
        claude_json = config_dir / ".claude.json"
    environment = dict(os.environ)
    for variable in unset_variables:
        _ = environment.pop(variable, None)
    environment.update(pane_environment)
    trust = herdr.claude_trust(folder, claude_json)
    if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
        reason = str(trust) if isinstance(trust, herdr.UnreadableTrustFile) else f"Claude has not trusted {folder}"
        trust_command = (f"cd {shlex.quote(str(folder))} && env -u CLAUDE_CONFIG_DIR "
                         + "-u CLAUDE_CODE_REMOTE_MEMORY_DIR -u CLAUDE_CODE_OAUTH_TOKEN claude"
                         if isinstance(selected, accounts.LegacyHomeLaunch)
                         else accounts.trust_command(folder, selected))
        if isinstance(trust, herdr.UnreadableTrustFile):
            print(f"herdr_sessions: {reason}. Run: {trust_command}", file=sys.stderr)
        else:
            rerun = _launch_rerun(verb, options, folder, claude_arguments)
            print(f"herdr_sessions: {reason}.\n"
                  + "Paste this, accept Claude's trust prompt, then exit Claude; the session then starts:\n"
                  + f"{trust_command} && {rerun}", file=sys.stderr)
        return 1
    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt = herdr.NoOpeningPrompt()
    if options.prompt_file:
        opening = herdr.OpeningPrompt(Path(options.prompt_file).read_text(),
                                      herdr.SessionTranscript(config_dir, options.session_id))
    purpose: launch_permission.LaunchPurpose = (
        launch_permission.RecordedSessionRestoreLaunchPurpose(options.session_id)
        if options.restore else launch_permission.NewWorkLaunchPurpose()
    )
    try:
        barrier = launch_permission.launch_barrier()
        _ = barrier.__enter__()
    except OSError as error:
        print(f"herdr_sessions: shutdown state unreadable: launch barrier: {error}", file=sys.stderr)
        return 1
    try:
        permission = launch_permission.launch_permission(environment, purpose)
        if isinstance(permission, launch_permission.BlockedByShutdown):
            print(f"herdr_sessions: {permission.reason}", file=sys.stderr)
            return 1
        if isinstance(permission, launch_permission.ShutdownStateUnreadable):
            print(f"herdr_sessions: shutdown state unreadable: {permission.detail}", file=sys.stderr)
            return 1
        shared = herdr.shared_session()
        _ = herdr.start_server(shared, 90_000, session_kind=session_kind)
        server_environment = herdr.server_environment(shared)
        for variable in sorted(unset_variables):
            if variable in server_environment:
                raise herdr.HerdrFailure(
                    f"herdr server {shared} carries {variable} from where it started, so a new tab would "
                    + "inherit it; start that server again from a shell without it"
                )
        taken = {pane.get("label") for pane in herdr.Herdr(shared).panes()}
        if options.name in taken:
            raise herdr.HerdrFailure(f"a tab named {options.name} is already open in herdr session {shared}")
        name = options.name or next(candidate for candidate in (
            herdr.generated_session_name(folder.name, str(suffix)) for suffix in itertools.count(1))
            if candidate not in taken)
        if verb == "resume":
            claude_arguments.extend(["--resume", options.session_id])
        claude_arguments.extend(["-n", name])
        launched = herdr.launch_agent(shared, ADHOC_SPACE, name, str(folder), pane_environment,
            kind="claude", agent=name, agent_arguments=claude_arguments, opening=opening, timeout_ms=90_000)
        if isinstance(launched, herdr.nix_env.Refused):
            raise herdr.HerdrFailure(launched.reason)
        pane, outcome = launched.pane, launched.outcome
        if isinstance(opening, herdr.OpeningPrompt) and not isinstance(outcome, herdr.PromptSubmitted):
            reason = outcome.reason if isinstance(outcome, herdr.PromptNotSubmitted) else "launcher supplied no submit proof"
            print(f"herdr_sessions: {name}: opening prompt not submitted: {reason}", file=sys.stderr)
            return 1
    finally:
        _ = barrier.__exit__(None, None, None)
    if verb == "start" or options.attach:
        _attach(pane)
    return 0


def claude_names() -> dict[str, str]:
    """The newest live Claude name in each pane of the shared herdr session."""
    shared = herdr.shared_session()
    newest: dict[str, SessionRecord] = {}
    directory = sessions_dir()
    if not directory.exists():
        return {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord) or not live_session(record):
            continue
        place = herdr.herdr_place_of(record["pid"])
        if not isinstance(place, herdr.HerdrPlace) or place.session != shared:
            continue
        if place.pane not in newest or record["updatedAt"] > newest[place.pane]["updatedAt"]:
            newest[place.pane] = record
    return {pane: record["name"] for pane, record in newest.items()}


def unit_roles() -> dict[str, str]:
    """Unit roles of the shared session's panes, from the marks inherited by processes in them."""
    shared = herdr.shared_session()
    roles: dict[str, str] = {}
    for process in herdr.herdr_processes():
        slug = process.marks.get("SHOWRUNNER_UNIT", "")
        unit = process.marks.get("SHOWRUNNER_UNIT_ID", "")
        if process.place.session == shared and herdr.has_unit_marks(process.marks):
            _ = roles.setdefault(process.place.pane, f"unit {slug}/{unit}")
    return roles


def _main(argv: list[str]) -> int:
    if argv[1:] in (["--help"], ["-h"]):
        print(USAGE + "\n\nlist --on <host>: list herdr sessions on that host over SSH."
              + "\nattach-remote <host> <name>: attach to that host's herdr session.")
        return 0
    remote_list = len(argv) == 4 and argv[1:3] == ["list", "--on"]
    remote_attach = len(argv) == 4 and argv[1] == "attach-remote" and bool(argv[3])
    if remote_list or remote_attach:
        target = argv[3] if remote_list else argv[2]
        try:
            host = RemoteHost(target)
        except ValueError as error:
            print(f"herdr_sessions: {error}", file=sys.stderr)
            return 2
        if remote_list:
            return _remote_list(host)
        try:
            return _attach_remote(host, argv[3])
        except (herdr.HerdrFailure, OSError, subprocess.TimeoutExpired) as error:
            print(f"herdr_sessions: {host.target}: attach failed: {' '.join(str(error).split())}", file=sys.stderr)
            return 1
    if len(argv) > 1 and argv[1] in ("start", "resume"):
        try:
            return _launch(argv[1], argv[2:])
        except (herdr.HerdrFailure, OSError, ValueError) as error:
            print(f"herdr_sessions: {error}", file=sys.stderr)
            return 1
    if argv[1:] not in (["list"], ["sort-tabs"]) and not (len(argv) == 3 and argv[1] in ("attach", "focus") and argv[2]):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        shared = herdr.shared_session()
        if not any(session["name"] == shared for session in herdr.live_sessions()):
            if argv[1] == "list":
                return 0
            print(f"herdr_sessions: herdr session {shared} is not running", file=sys.stderr)
            return 1
        if argv[1] == "sort-tabs":
            for label in herdr.Herdr(shared).sort_tabs().labels:
                print(f"herdr_sessions: {label}: tab with several panes; order kept")
            return 0
        panes = herdr.Herdr(shared).panes()
        if argv[1] in ("attach", "focus"):
            name = argv[2]
            pane = _tab_pane(name)
            if not pane:
                names = ", ".join(sorted({row.get("label") or row["pane_id"] for row in panes})) or "none"
                print(f"herdr_sessions: no tab named {name} in herdr session {shared}; open tabs: {names}",
                      file=sys.stderr)
                return 1
            if argv[1] == "focus":
                herdr.Herdr(shared).focus_tab_of(pane)
                print(shared)
                return 0
            _attach(pane)
        names = claude_names()
        roles = unit_roles()
        showrunner_names = {runner["session"] for runner in registered_showrunners() if runner["session"]}
        for row in panes:
            pane = row["pane_id"]
            claude = names.get(pane, "")
            role = roles.get(pane, "showrunner" if claude and claude in showrunner_names else "standalone")
            print(f"{row.get("label") or pane}\t{role}\t{claude}")
        return 0
    except (herdr.HerdrFailure, OSError) as error:
        print(f"herdr_sessions: {error}", file=sys.stderr)
        return 3


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("herdr_sessions: stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
