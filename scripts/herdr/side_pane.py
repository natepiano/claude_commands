"""Open, message, and close a consultation beside its caller."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal, cast, final, get_args

from scripts.herdr import herdr, worker_panes
from scripts.lib import conf, models_registry, nix_env
from scripts.production import staff
from scripts.shutdown import stop_resume

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


def _find(client: herdr.Herdr, panes: list[herdr.PaneIdentity], caller: CallerPane, label: str) -> SidePane:
    matches = [pane for pane in panes if pane["pane_id"] != caller.pane and pane.get("tab_id") == caller.tab and pane.get("label") == label]
    if not matches:
        return AbsentSidePane()
    if len(matches) != 1:
        raise SidePaneRefused(f"{label}: duplicate labelled panes")
    pane = matches[0]
    terminal = pane.get("terminal_id", "")
    if not terminal or sum(item.get("terminal_id") == terminal for item in panes) != 1:
        raise SidePaneRefused(f"{label}: terminal identity is missing or duplicated")
    try:
        activity = client.pane_activity(pane["pane_id"])
        if isinstance(activity, herdr.PaneAtShell):
            return StoppedSidePane(pane["pane_id"], terminal)
        return OpenSidePane(pane["pane_id"], terminal)
    except (herdr.HerdrFailure, OSError, ValueError) as error:
        return _note(client, pane["pane_id"], str(error))


def _context() -> tuple[herdr.Herdr, list[herdr.PaneIdentity], CallerPane]:
    client = herdr.Herdr(herdr.shared_session())
    panes = client.panes()
    return client, panes, _caller(panes)


def open(label: str, *, question: str = "", cwd: Path | str = ".") -> SidePane:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, (OpenSidePane, UnknownSidePane)):
        return current
    policy = _policy()
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
    launched = herdr.launch_prepared_agent(client.session, destination, prepared, policy.settings.kind,
        worker_panes.launch_arguments(policy, "", cwd), herdr.NoOpeningPrompt(),
        int(config.number("PANE_AGENT_START_SECONDS") * 1000), agent=label)
    if isinstance(launched.outcome, herdr.PromptNotSubmitted):
        return _note(client, launched.pane, launched.outcome.reason)
    fresh = client.panes()
    result = _find(client, fresh, caller, label)
    if question and isinstance(result, OpenSidePane):
        # The agent just launched in this call, so it holds no draft and needs no composer check.
        client.prompt_terminal(result.pane, result.terminal, f"{question}\n{caller.reply_command}")
    return result


def _policy() -> worker_panes.LaunchPolicy:
    settings = models_registry.launch_settings("ask_a_friend", "consultation")
    try:
        return worker_panes.worker_policy(settings)
    except conf.Unusable as error:
        raise SidePaneRefused(str(error)) from error


def send(label: str, text: str) -> SidePane:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, OpenSidePane):
        client.prompt_terminal(current.pane, current.terminal,
            f"{text}\n{caller.reply_command}")
    return current


def close(label: str) -> SidePane | bool:
    client, panes, caller = _context()
    current = _find(client, panes, caller, label)
    if isinstance(current, (AbsentSidePane, UnknownSidePane)):
        return current
    if isinstance(current, OpenSidePane):
        process = client.pane_agent_process(current.pane, current.terminal)
        if not herdr.check_target(process, action="signal"):
            return _note(client, current.pane, "agent process is unproved")
        os.kill(process.pid, signal.SIGTERM)
        deadline = monotonic() + stop_resume.stop_grace_seconds()
        while not isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _note(client, current.pane, "agent has not ended")
            _ = Event().wait(min(0.1, remaining))
    fresh = [pane for pane in client.panes() if pane.get("terminal_id") == current.terminal]
    if len(fresh) != 1 or fresh[0]["pane_id"] != current.pane:
        return _note(client, current.pane, "terminal moved, disappeared, or duplicated before close")
    if not isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
        return _note(client, current.pane, "pane has foreground work before close")
    return client.close_retained_pane(current.pane, terminal=current.terminal)


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
            question = Path(args.question_file).read_text() if args.question_file else ""
            result = open(args.label, question=question, cwd=Path(args.cwd))
        elif args.command == "send":
            result = send(args.label, Path(args.message_file).read_text())
        else:
            result = close(args.label)
        if isinstance(result, UnknownSidePane) or result is False:
            return worker_panes.ExitCode.UNCERTAIN
        if args.command != "close" and not isinstance(result, OpenSidePane):
            raise SidePaneRefused(f"{args.label}: consultation is not running")
        print("done")
        return 0
    except SidePaneRefused as error:
        print(str(error), file=sys.stderr)
        return worker_panes.ExitCode.REFUSED
    except (herdr.HerdrFailure, conf.Unusable, OSError, ValueError) as error:
        print(f"anomaly: {args.label}: {error}; pane retained", file=sys.stderr)
        return worker_panes.ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
