#!/usr/bin/env python3
"""List, attach, start or resume standalone Claude sessions in the shared herdr session.

Each standalone session uses a workspace with its tab name.
This module starts no separate herdr session.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import itertools
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.herdr import herdr
from scripts.herdr import herdr_types

from scripts.message.sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session, sessions_dir  # noqa: E402

from scripts.production.showrunners import registered_showrunners, require_complete  # noqa: E402

from scripts.accounts import accounts  # noqa: E402
from scripts.whoami import account  # noqa: E402
from scripts.production import staff  # noqa: E402

USAGE = "usage: herdr_sessions.py list [--on <host>] | sort-tabs | attach <name> | focus <name> | attach-remote <host> <name> | start [--name N] [--cwd D] [-- <claude args>] | resume <name> --session-id ID --cwd D [--config-dir D] [--restore] [--prompt-file P] [--attach] [-- <claude args>]"
REMOTE_LIST_TIMEOUT = 15


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
    if not result.stdout.strip():
        print(f"herdr_sessions: {host.target}: remote session is unknown", file=sys.stderr)
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
    try:
        herdr.Herdr(shared).focus_tab_of(pane)
    except (herdr_types.HerdrFailure, OSError) as error:
        print(f"herdr_sessions: focus failed: {error}", file=sys.stderr)
    binary = herdr.herdr_binary()
    os.execvp(binary, [binary, "session", "attach", shared])


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
    else:
        config_dir = selected.dir
        pane_environment = account.named_account_environment(config_dir)
    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt = herdr.NoOpeningPrompt()
    if options.prompt_file:
        try:
            opening = herdr.OpeningPrompt(Path(options.prompt_file).read_text(),
                                          herdr.SessionTranscript(config_dir, options.session_id))
        except OSError as error:
            print(f"herdr_sessions: {options.name}: prompt file is unreadable: {error}", file=sys.stderr)
    shared = herdr.shared_session()
    _ = herdr.restore_server(shared, 90_000, session_kind=session_kind)
    panes = herdr.Herdr(shared).panes()
    taken = {pane.get("label") for pane in panes}
    name = options.name or next(candidate for candidate in (
        herdr.generated_session_name(folder.name, str(suffix)) for suffix in itertools.count(1))
        if candidate not in taken)
    if verb == "resume":
        claude_arguments.extend(["--resume", options.session_id])
    claude_arguments.extend(["-n", name])
    launched = herdr.launch_agent(shared, name, name, str(folder), pane_environment,
        kind="claude", agent=name, agent_arguments=claude_arguments, opening=opening, timeout_ms=90_000,
        unset_environment=frozenset(unset_variables))
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr_types.HerdrFailure(launched.reason)
    pane, outcome = launched.pane, launched.outcome
    if isinstance(opening, herdr.OpeningPrompt) and not isinstance(outcome, herdr.PromptSubmitted):
        reason = outcome.reason if isinstance(outcome, herdr.PromptNotSubmitted) else "launcher supplied no submit proof"
        print(f"herdr_sessions: {name}: opening prompt not submitted: {reason}", file=sys.stderr)
    if verb == "start" or options.attach:
        _attach(pane)
    return 0


def claude_sessions() -> dict[str, SessionRecord]:
    """Return the newest live Claude record for each pane in the shared session."""
    shared = herdr.shared_session()
    newest: dict[str, SessionRecord] = {}
    directory = sessions_dir()
    if not directory.exists():
        return {}
    places = herdr.herdr_places()
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord) or not live_session(record):
            continue
        place = places.get(record["pid"])
        if not isinstance(place, herdr.HerdrPlace) or place.session != shared:
            continue
        if place.pane not in newest or record["updatedAt"] > newest[place.pane]["updatedAt"]:
            newest[place.pane] = record
    return newest


def unit_roles(client: herdr.Herdr, panes: list[herdr.PaneIdentity], *,
               record: staff.StaffSnapshot) -> dict[str, str]:
    """Classify live unit panes using the same staff join as tab ordering."""
    from scripts.production.session_labels import StaffLabels

    classifier = herdr.UnitTabClassifier(
        client.session, herdr.herdr_processes(membership=StaffLabels.for_record(record)), record)
    roles: dict[str, str] = {}
    for pane in panes:
        unit = classifier.classify([pane["pane_id"]])
        if isinstance(unit, herdr.UnitTab):
            roles[pane["pane_id"]] = f"unit {unit.parent.name}/{unit.row.name}"
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
        except (herdr_types.HerdrFailure, OSError, subprocess.TimeoutExpired) as error:
            print(f"herdr_sessions: {host.target}: attach failed: {' '.join(str(error).split())}", file=sys.stderr)
            return 1
    if len(argv) > 1 and argv[1] in ("start", "resume"):
        try:
            return _launch(argv[1], argv[2:])
        except (herdr_types.HerdrFailure, OSError, ValueError) as error:
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
            _ = herdr.restore_server(shared, 90_000, session_kind=herdr.security_session())
        if argv[1] == "sort-tabs":
            for label in herdr.Herdr(shared).sort_tabs().labels:
                print(f"herdr_sessions: {label}: tab with several panes; order kept")
            return 0
        if argv[1] in ("attach", "focus"):
            panes = herdr.Herdr(shared).panes()
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
        with herdr.pane_snapshot() as read_panes:
            panes = read_panes(shared)
            sessions = claude_sessions()
            membership = staff.initial_snapshot()
            roles = unit_roles(herdr.Herdr(shared), panes, record=membership)
            documents = {runner["doc"] for runner in require_complete(registered_showrunners(membership=membership))}
            showrunner_ids = {row.id for row in membership.find(kind="showrunner")
                              if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and str(row.doc.path) in documents}
            for row in panes:
                pane = row["pane_id"]
                current = sessions.get(pane)
                claude = current["name"] if current is not None else ""
                owner = membership.by_current_session(current["sessionId"]) if current is not None else None
                role = roles.get(pane, "showrunner" if owner is not None and owner.id in showrunner_ids else "standalone")
                print(f"{row.get("label") or pane}\t{role}\t{claude}")
            return 0
    except (herdr_types.HerdrFailure, OSError) as error:
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
