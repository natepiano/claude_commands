"""Open, message, and close a consultation beside its caller."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal, cast, final, get_args

from scripts.herdr import herdr, pane_launch, worker_panes
from scripts.herdr import herdr_types
from scripts.lib import conf, nix_env
from scripts.production import staff
from scripts.agents import stop_resume

SideCommand = Literal["open", "send", "close"]


class SidePaneRefused(ValueError):
    """The pane structure cannot identify one consultation."""


@dataclass(frozen=True)
class CallerPane:
    identity: str
    kind: Literal["claude", "codex"]
    pane: str
    tab: str

    @property
    def address(self) -> str:
        return f"{'codex' if self.kind == 'codex' else 'session'}:{self.identity}"

    @property
    def reply_command(self) -> str:
        return f'Reply with ~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {self.address} --text "<answer>".'


@dataclass(frozen=True)
class LocatedSidePane:
    pane: str
    terminal: str


@dataclass(frozen=True)
class OpenSidePane(LocatedSidePane):
    pass


@dataclass(frozen=True)
class StoppedSidePane(LocatedSidePane):
    pass


@dataclass(frozen=True)
class AbsentSidePane:
    pass


@dataclass(frozen=True)
class UnknownSidePane:
    pass


SidePane = OpenSidePane | StoppedSidePane | AbsentSidePane | UnknownSidePane


def _note(client: herdr.Herdr, pane: str, reason: str) -> UnknownSidePane:
    print(f"herdr {client.session}:{pane}: {reason}; pane retained", file=sys.stderr)
    return UnknownSidePane()


def _caller(panes: list[herdr.PaneIdentity]) -> CallerPane:
    identity = staff.runtime_id()
    kind = "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"
    pane = next((pane for pane in panes if pane["pane_id"] == os.environ.get("HERDR_PANE_ID", "")), None)
    tab = pane.get("tab_id", "") if pane is not None else ""
    if pane is None or not tab:
        raise SidePaneRefused("caller pane is missing or has no tab")
    return CallerPane(identity, kind, pane["pane_id"], tab)


def _helper_process(client: herdr.Herdr, pane: herdr.PaneIdentity) -> bool:
    """Exclude another launch or a staffed native owner from name recovery."""
    identity = pane["pane_id"]
    entries = pane_launch.read(pane_launch.record_directory() / f"{client.session}.json")
    entry = entries.get(identity)
    info = herdr.json_object(client.result("pane", "process-info", "--pane", identity).get("process_info"),
                             "consultation process info")
    for value in herdr.json_items(info.get("foreground_processes"), "consultation processes"):
        process = herdr.json_object(value, "consultation process")
        arguments = [str(argument) for argument in herdr.json_items(process.get("argv"), "consultation arguments")]
        kind = "codex" if herdr.is_codex_process(arguments) else "claude" if herdr.is_claude_process(arguments) else ""
        if not kind:
            continue
        try:
            native = herdr.pane_native_process(info, kind, pane)
        except (herdr.CodexPaneStarting, herdr.CodexSessionPending):
            # A pending consultation restart can replace its previous native launch.
            return entry is None or "staff_id" not in entry and bool(entry.get("name"))
        if entry is not None and "staff_id" not in entry and (
                entry.get("session_id") == native.current_identity
                or entry.get("pid") == process.get("pid") and entry.get("proc_start") == native.started):
            return True
        record = staff.initial_snapshot()
        if record.by_member_reference(native.current_identity) is not None:
            return False
        if entry is None:
            return False
        return pane_launch.matching_launch({identity: entry}, herdr.HerdrPlace(client.session, identity),
                                          native.current_identity) is not None
    return entry is None


def _find(client: herdr.Herdr, panes: list[herdr.PaneIdentity], caller: CallerPane, label: str) -> SidePane:
    candidates: list[herdr.PaneIdentity] = []
    activities: dict[str, herdr.PaneAtShell | herdr.PaneRunning] = {}
    entries = pane_launch.read(pane_launch.record_directory() / f"{client.session}.json")
    for pane in panes:
        if pane["pane_id"] == caller.pane or pane.get("tab_id") != caller.tab:
            continue
        identity = pane["pane_id"]
        entry = entries.get(identity)
        if entry is not None:
            if "staff_id" in entry or entry.get("name") != label:
                continue
        # Consultation starts use names only before a native identity and launch record exist.
        elif pane.get("label") != label:
            continue
        try:
            activity = client.pane_activity(identity)
            if isinstance(activity, herdr.PaneAtShell):
                eligible = True
            else:
                eligible = _helper_process(client, pane)
            if eligible:
                candidates.append(pane)
                activities[identity] = activity
        except (herdr_types.HerdrFailure, OSError, ValueError) as error:
            return _note(client, identity, str(error))
    try:
        pane = worker_panes.selected_pane(panes, candidates, label)
    except ValueError as error:
        raise SidePaneRefused(str(error)) from error
    if isinstance(pane, herdr.NoPane):
        return AbsentSidePane()
    identity = str(pane["pane_id"])
    terminal = str(pane["terminal_id"])
    if isinstance(activities[identity], herdr.PaneAtShell):
        return StoppedSidePane(identity, terminal)
    return OpenSidePane(identity, terminal)


def _context() -> tuple[herdr.Herdr, list[herdr.PaneIdentity], CallerPane]:
    client = herdr.Herdr(herdr.shared_session())
    panes = client.panes()
    return client, panes, _caller(panes)


def open(label: str, policy: worker_panes.LaunchPolicy, *, question: str = "", cwd: Path | str = ".",
         delivery: worker_panes.ReplyToCaller | worker_panes.TranscriptReply = worker_panes.REPLY_TO_CALLER) -> SidePane:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, (OpenSidePane, UnknownSidePane)):
        return current
    config = conf.read("direct")
    environment = worker_panes.launch_environment(policy, worker_panes.ConfiguredWorkerAccount.CURRENT, False)
    if isinstance(environment, worker_panes.StartRefused):
        raise SidePaneRefused(environment.detail)
    prepared = herdr.prepare_agent_environment(str(cwd), environment, frozenset({"STAFF_ID"}))
    if isinstance(prepared, nix_env.Refused):
        raise SidePaneRefused(str(prepared))
    if isinstance(current, StoppedSidePane):
        destination = herdr.ExistingPane(current.pane, current.terminal)
    else:
        ratio = config.number("WORKER_SPLIT_RATIO")
        destination = herdr.SplitPane(caller.pane, "right", ratio, label)
    arguments = worker_panes.launch_arguments(policy, "", cwd)
    if policy.settings.kind == "claude":
        arguments.extend(["-n", label])
    launched = herdr.launch_prepared_agent(client.session, destination, prepared, policy.settings.kind,
        arguments, herdr.NoOpeningPrompt(),
        int(config.number("PANE_AGENT_START_SECONDS") * 1000), agent=label)
    if isinstance(launched.outcome, herdr.PromptNotSubmitted):
        return _note(client, launched.pane, launched.outcome.reason)
    fresh = client.panes()
    result = _find(client, fresh, caller, label)
    if question and isinstance(result, OpenSidePane):
        # The agent just launched in this call, so it holds no draft and needs no composer check.
        text = f"{question}\n{caller.reply_command}" if isinstance(delivery, worker_panes.ReplyToCaller) else question
        client.prompt_terminal(result.pane, result.terminal, text)
        if policy.settings.kind == "codex":
            deadline = monotonic() + config.number("PANE_AGENT_START_SECONDS")
            while True:
                try:
                    _ = client.native_pane_process(result.pane, "codex", max(0, deadline - monotonic()))
                    entry = client.codex_launch(result.pane)
                except (herdr.CodexPaneStarting, herdr.CodexSessionPending) as error:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return _note(client, result.pane, str(error))
                    _ = Event().wait(min(0.1, remaining))
                else:
                    entry["name"] = label
                    pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", entry)
                    break
    return result


def send(label: str, text: str) -> SidePane:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, OpenSidePane):
        client.prompt_terminal(current.pane, current.terminal,
            f"{text}\n{caller.reply_command}")
    return current


@dataclass(frozen=True)
class ClosedSidePane:
    pass


@dataclass(frozen=True)
class SidePaneCloseUnavailable:
    reason: str


def _close_unavailable(client: herdr.Herdr, pane: str, reason: str) -> SidePaneCloseUnavailable:
    _ = _note(client, pane, reason)
    return SidePaneCloseUnavailable(reason)


def close(label: str) -> ClosedSidePane | SidePaneCloseUnavailable:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, AbsentSidePane):
        return ClosedSidePane()
    if isinstance(current, UnknownSidePane):
        return SidePaneCloseUnavailable("pane evidence is unavailable")
    if isinstance(current, OpenSidePane):
        process = client.pane_agent_process(current.pane, current.terminal)
        if not herdr.check_target(process, action="signal"):
            return _close_unavailable(client, current.pane, "agent process is unproved")
        os.kill(process.pid, signal.SIGTERM)
        deadline = monotonic() + stop_resume.stop_grace_seconds()
        while not isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _close_unavailable(client, current.pane, "agent has not ended")
            _ = Event().wait(min(0.1, remaining))
    fresh = [pane for pane in client.panes() if pane.get("terminal_id") == current.terminal]
    if len(fresh) != 1 or fresh[0]["pane_id"] != current.pane:
        return _close_unavailable(client, current.pane, "terminal moved, disappeared, or duplicated before close")
    if not isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
        return _close_unavailable(client, current.pane, "pane has foreground work before close")
    if client.close_retained_pane(current.pane, terminal=current.terminal):
        return ClosedSidePane()
    return SidePaneCloseUnavailable("pane close is unproved")


@final
class Arguments(argparse.Namespace):
    command: SideCommand = "open"
    label: str = ""
    question_file: str = ""
    message_file: str = ""
    cwd: str = ""


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in cast(tuple[SideCommand, ...], get_args(SideCommand)):
        subcommand = commands.add_parser(command)
        _ = subcommand.add_argument("label")
        if command == "open":
            _ = subcommand.add_argument("--question-file", default="")
            _ = subcommand.add_argument("--cwd", default=str(Path.cwd()))
        elif command == "send":
            _ = subcommand.add_argument("--message-file", required=True)
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        if args.command == "open":
            command = ["bash", str(Path(__file__).resolve().parents[1] / "direct/assign.sh"),
                       "--role", "implement", "--target", "consultation-pane", "--policy", "friend",
                       "--new-worker", args.label, "--cwd", args.cwd]
            if args.question_file:
                command += ["--prompt", args.question_file]
            return subprocess.run(command, check=False).returncode
        elif args.command == "send":
            result = send(args.label, Path(args.message_file).read_text())
        else:
            result = close(args.label)
        if isinstance(result, (UnknownSidePane, SidePaneCloseUnavailable)):
            return worker_panes.ExitCode.UNCERTAIN
        if args.command != "close" and not isinstance(result, OpenSidePane):
            raise SidePaneRefused(f"{args.label}: consultation is not running")
        print("done")
        return 0
    except SidePaneRefused as error:
        print(str(error), file=sys.stderr)
        return worker_panes.ExitCode.REFUSED
    except (herdr_types.HerdrFailure, conf.Unusable, OSError, ValueError) as error:
        print(f"anomaly: {args.label}: {error}; pane retained", file=sys.stderr)
        return worker_panes.ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
