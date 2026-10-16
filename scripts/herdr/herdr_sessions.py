#!/usr/bin/env python3
"""List, attach, start or resume standalone Claude sessions in herdr."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import herdr

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "message"))
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session, sessions_dir  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "production"))
from showrunners import registered_showrunners  # noqa: E402

sys.path.insert(0, str(SCRIPTS.parent))
from scripts.shutdown import launch_permission  # noqa: E402
from scripts.accounts import accounts  # noqa: E402
from scripts.whoami import account  # noqa: E402

USAGE = "usage: herdr_sessions.py list | attach <name> | start [--name N] [--cwd D] [-- <claude args>] | resume <name> --session-id ID --cwd D [--config-dir D] [--restore] [--prompt-file P] [--attach] [-- <claude args>]"


class LaunchOptions(argparse.Namespace):
    name: str | None = None
    cwd: str = ""
    session_id: str = ""
    config_dir: str = ""
    restore: bool = False
    prompt_file: str = ""
    attach: bool = False


def _attach(name: str) -> None:
    binary = herdr.herdr_binary()
    os.execvp(binary, [binary, "session", "attach", name])


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
        opening = herdr.OpeningPrompt(Path(options.prompt_file).read_text())
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
        suffix = 1
        while True:
            name = options.name or herdr.generated_session_name(folder.name, str(suffix))
            try:
                herdr.start_new_server(name, 90_000)
                break
            except herdr.SessionTaken:
                if options.name:
                    raise
                suffix += 1
        pane = herdr.open_pane(name, name, str(folder), pane_environment)
        server_environment = herdr.server_environment(name)
        for variable in sorted(unset_variables):
            if variable in server_environment:
                raise herdr.HerdrFailure(
                    f"herdr server {name} carries {variable} from where it started; "
                    + f"stop it with herdr --session {name} server stop and try again"
                )
        if verb == "resume":
            claude_arguments.extend(["--resume", options.session_id])
        claude_arguments.extend(["-n", name])
        herdr.start_agent(name, pane, kind="claude", agent_arguments=claude_arguments,
                          opening=opening, timeout_ms=90_000)
    finally:
        _ = barrier.__exit__(None, None, None)
    if verb == "start" or options.attach:
        _attach(name)
    return 0


def claude_names() -> dict[str, str]:
    """The newest live Claude name in each herdr session."""
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
        if not isinstance(place, herdr.HerdrPlace):
            continue
        if place.session not in newest or record["updatedAt"] > newest[place.session]["updatedAt"]:
            newest[place.session] = record
    return {session: record["name"] for session, record in newest.items()}


def unit_roles() -> dict[str, str]:
    """Unit roles come from the marks inherited by processes in herdr panes."""
    roles: dict[str, str] = {}
    for process in herdr.herdr_processes():
        slug = process.marks.get("SHOWRUNNER_UNIT", "")
        unit = process.marks.get("SHOWRUNNER_UNIT_ID", "")
        if slug and unit:
            _ = roles.setdefault(process.place.session, f"unit {slug}/{unit}")
    return roles


def _main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] in ("start", "resume"):
        try:
            return _launch(argv[1], argv[2:])
        except (herdr.HerdrFailure, OSError, ValueError) as error:
            print(f"herdr_sessions: {error}", file=sys.stderr)
            return 1
    if argv[1:] != ["list"] and not (len(argv) == 3 and argv[1] == "attach" and argv[2]):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        running = herdr.live_sessions()
        if argv[1] == "attach":
            name = argv[2]
            if not any(session["name"] == name for session in running):
                names = ", ".join(session["name"] for session in running) or "none"
                print(f"herdr_sessions: {name} is not running; running sessions: {names}", file=sys.stderr)
                return 1
            _attach(name)
        if not running:
            return 0
        names = claude_names()
        roles = unit_roles()
        showrunner_names = {runner["session"] for runner in registered_showrunners() if runner["session"]}
        for session in running:
            name = session["name"]
            claude = names.get(name, "")
            role = roles.get(name, "showrunner" if claude and claude in showrunner_names else "standalone")
            print(f"{name}\t{role}\t{claude}")
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
