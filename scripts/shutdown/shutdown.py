#!/usr/bin/env python3
"""Inspect and, in later phases, stop or restart one Claude account's sessions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import Account, claude_account, label_for
from agent_accounts import claude_config_dir
from agent_notes import AGENTS_DIR, read_note
from inventory import Checkout, CodexServer, Host, Inventory, Session, inventory
from remote import other_machine, run_remote


class CommandLine(argparse.Namespace):
    command: str = ""
    account: str | None = None
    as_json: bool = False
    here: bool = False


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]


class UnavailableMachine(TypedDict):
    """A reachable peer that could not provide a usable inventory."""

    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str


MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine


class UnknownAccount(ValueError):
    """The requested text is neither a Claude note label nor a login."""


class InvalidInventoryResponse(ValueError):
    """A peer response does not satisfy the complete inventory contract."""


def _requested_account(requested: str | None) -> Account | None:
    if requested is None:
        return claude_account(claude_config_dir())
    notes_dir = Path(os.environ.get("AGENT_NOTES_DIR", str(AGENTS_DIR)))
    folded = requested.casefold()
    for path in sorted(notes_dir.glob("*.md")):
        stem = path.stem.casefold()
        if not stem.startswith("claude") or stem != folded:
            continue
        note = read_note(path)
        login = None if note is None else note.get("login")
        if isinstance(login, str) and login:
            return Account("claude", login, path.stem)
    if "@" in requested:
        return Account("claude", requested, label_for("claude", requested))
    raise UnknownAccount(
        f"unknown account {requested}: give a note label such as claude 2, or a login"
    )


def _object(value: object, place: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidInventoryResponse(f"{place} must be an object")
    return cast(dict[str, object], value)


def _required(data: dict[str, object], key: str, place: str) -> object:
    if key not in data:
        raise InvalidInventoryResponse(f"{place} is missing {key}")
    return data[key]


def _string(value: object, place: str) -> str:
    if not isinstance(value, str):
        raise InvalidInventoryResponse(f"{place} must be a string")
    return value


def _integer(value: object, place: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInventoryResponse(f"{place} must be an integer")
    return value


def _nullable_string(value: object, place: str) -> str | None:
    if value is None:
        return None
    return _string(value, place)


def _strings(value: object, place: str) -> list[str]:
    if not isinstance(value, list):
        raise InvalidInventoryResponse(f"{place} must be a list")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise InvalidInventoryResponse(f"{place} must contain only strings")
    return cast(list[str], items)


def _host(value: object, place: str) -> Host:
    data = _object(value, place)
    kind = _string(_required(data, "kind", place), f"{place}.kind")
    allowed = ("unit", "tmux", "ghostty", "zed", "terminal", "unknown")
    if kind not in allowed:
        raise InvalidInventoryResponse(f"{place}.kind is invalid")
    host: Host = {"kind": kind}
    if "production" in data:
        host["production"] = _string(data["production"], f"{place}.production")
    if "unit" in data:
        host["unit"] = _string(data["unit"], f"{place}.unit")
    if "doc" in data:
        host["doc"] = _string(data["doc"], f"{place}.doc")
    if "plan" in data:
        host["plan"] = _string(data["plan"], f"{place}.plan")
    if "tmux_session" in data:
        host["tmux_session"] = _string(
            data["tmux_session"], f"{place}.tmux_session"
        )
    if "desktop" in data:
        host["desktop"] = _string(data["desktop"], f"{place}.desktop")
    if "window_shell" in data:
        host["window_shell"] = _integer(
            data["window_shell"], f"{place}.window_shell"
        )
    return host


def _checkout(value: object, place: str) -> Checkout | None:
    if value is None:
        return None
    data = _object(value, place)
    ahead_value = _required(data, "ahead", place)
    ahead = None if ahead_value is None else _integer(ahead_value, f"{place}.ahead")
    return {
        "path": _string(_required(data, "path", place), f"{place}.path"),
        "branch": _string(_required(data, "branch", place), f"{place}.branch"),
        "head": _string(_required(data, "head", place), f"{place}.head"),
        "ahead": ahead,
        "dirty": _strings(_required(data, "dirty", place), f"{place}.dirty"),
    }


def _codex_server(value: object, place: str) -> CodexServer:
    data = _object(value, place)
    return {
        "run_dir": _string(_required(data, "run_dir", place), f"{place}.run_dir"),
        "pid": _integer(_required(data, "pid", place), f"{place}.pid"),
        "busy_seats": _strings(
            _required(data, "busy_seats", place), f"{place}.busy_seats"
        ),
    }


def _session(value: object, place: str) -> Session:
    data = _object(value, place)
    kind = _string(_required(data, "kind", place), f"{place}.kind")
    allowed = ("showrunner", "unit", "seat", "top-level")
    if kind not in allowed:
        raise InvalidInventoryResponse(f"{place}.kind is invalid")
    servers_value = _required(data, "codex_servers", place)
    if not isinstance(servers_value, list):
        raise InvalidInventoryResponse(f"{place}.codex_servers must be a list")
    servers = [
        _codex_server(server, f"{place}.codex_servers[{index}]")
        for index, server in enumerate(cast(list[object], servers_value))
    ]
    return {
        "session_id": _string(
            _required(data, "session_id", place), f"{place}.session_id"
        ),
        "pid": _integer(_required(data, "pid", place), f"{place}.pid"),
        "name": _string(_required(data, "name", place), f"{place}.name"),
        "cwd": _string(_required(data, "cwd", place), f"{place}.cwd"),
        "kind": kind,
        "status": _string(_required(data, "status", place), f"{place}.status"),
        "host": _host(_required(data, "host", place), f"{place}.host"),
        "model": _nullable_string(
            _required(data, "model", place), f"{place}.model"
        ),
        "checkout": _checkout(
            _required(data, "checkout", place), f"{place}.checkout"
        ),
        "run_dirs": _strings(
            _required(data, "run_dirs", place), f"{place}.run_dirs"
        ),
        "codex_servers": servers,
        "timers": _strings(_required(data, "timers", place), f"{place}.timers"),
        "owner": _nullable_string(
            _required(data, "owner", place), f"{place}.owner"
        ),
    }


def _remote_inventory(text: str) -> Inventory:
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError):
        raise InvalidInventoryResponse("response is not valid JSON") from None
    data = _object(value, "inventory")
    sessions_value = _required(data, "sessions", "inventory")
    if not isinstance(sessions_value, list):
        raise InvalidInventoryResponse("inventory.sessions must be a list")
    sessions = [
        _session(session, f"inventory.sessions[{index}]")
        for index, session in enumerate(cast(list[object], sessions_value))
    ]
    return {
        "machine": _string(
            _required(data, "machine", "inventory"), "inventory.machine"
        ),
        "login": _string(_required(data, "login", "inventory"), "inventory.login"),
        "label": _string(_required(data, "label", "inventory"), "inventory.label"),
        "sessions": sessions,
        "unknown": _strings(
            _required(data, "unknown", "inventory"), "inventory.unknown"
        ),
    }


def _host_text(host: Host) -> str:
    match host["kind"]:
        case "unit":
            return f"unit {host.get('production', '?')}/{host.get('unit', '?')}"
        case "tmux":
            name = host.get("tmux_session")
            return f"tmux {name}" if name else "tmux"
        case "ghostty" | "zed":
            desktop = host.get("desktop")
            return f"{host['kind']} on {desktop}" if desktop else host["kind"]
        case kind:
            return kind


def _checkout_text(checkout: Checkout | None) -> str:
    if checkout is None:
        return "not a checkout"
    branch = checkout["branch"] or checkout["head"][:12]
    upstream = f"ahead {checkout['ahead']}" if checkout["ahead"] is not None else "no upstream"
    return f"{branch} {upstream}, {len(checkout['dirty'])} dirty"


def _session_lines(session: Session) -> list[str]:
    summary = f"  {session['kind']} {session['name']} · {_host_text(session['host'])}"
    summary += f" · {session['status']} · {_checkout_text(session['checkout'])}"
    lines = [summary]
    for server in session["codex_servers"]:
        activity = (
            "busy: " + ", ".join(server["busy_seats"])
            if server["busy_seats"]
            else "idle"
        )
        lines.append(f"    Codex {server['run_dir']} · pid {server['pid']} · {activity}")
    lines.extend(f"    timer {timer}" for timer in session["timers"])
    return lines


def _inventory_lines(report: Inventory) -> list[str]:
    lines = [f"{report['machine']}:"]
    for session in report["sessions"]:
        lines.extend(_session_lines(session))
    lines.extend(f"  unknown account · {unknown}" for unknown in report["unknown"])
    if not report["sessions"] and not report["unknown"]:
        lines.append(f"  no sessions on {report['label']}")
    return lines


def _unavailable(machine: str, rc: int, reason: str) -> UnavailableMachine:
    return {
        "machine": machine,
        "status": "unavailable",
        "rc": rc,
        "reason": reason.strip() or "remote command failed",
    }


def _status(
    selected: Account, here: bool
) -> tuple[list[MachineStatusReport], list[str]]:
    local = inventory(selected.login)
    reports: list[MachineStatusReport] = [local]
    lines = _inventory_lines(local)
    if here:
        return reports, lines

    status, output = run_remote(["status", selected.login, "--json", "--here"])
    machine = other_machine()
    if status == 255:
        reports.append({"machine": machine, "unreachable": True})
        lines.append(f"{machine}: unreachable")
        return reports, lines
    if status != 0:
        reports.append(_unavailable(machine, status, output))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    try:
        remote = _remote_inventory(output)
    except InvalidInventoryResponse as error:
        reports.append(_unavailable(machine, status, str(error)))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    reports.append(remote)
    lines.extend(_inventory_lines(remote))
    return reports, lines


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
    options = cast(CommandLine, parser.parse_args(arguments))

    try:
        selected = _requested_account(options.account)
    except UnknownAccount as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    if selected is None:
        print("shutdown: this process's Claude account is unreadable", file=sys.stderr)
        return 2
    reports, lines = _status(selected, options.here)
    if options.as_json:
        print(json.dumps(reports[0] if options.here else reports, sort_keys=True))
    else:
        print(f"{selected.label} · {selected.login}")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
