#!/usr/bin/env python3
"""Register a showrunner's update jobs and write its production time and log."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

from add_unit import Production, Refusal, cell_value, read_production, unit_table
from merge_checkpoint import NoMerge, merge_branch_history, report

if TYPE_CHECKING:
    from ..hooks.showrunner_footer import FooterState, footer_state
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    from showrunner_footer import FooterState, footer_state
from dailies_render import footer_main, read_outstanding


class InstancePresent(NamedTuple):
    output: str


class InstanceAbsent(NamedTuple):
    pass


class OutstandingItem(TypedDict):
    since: str
    text: str
    after: str


class UnitJudgment(NamedTuple):
    unit: str
    phase: str
    wait: str


class OpenForUserItem(NamedTuple):
    text: str
    after: str


class KeepOutstanding(NamedTuple):
    pass


class ReplaceOutstanding(NamedTuple):
    items: tuple[OpenForUserItem, ...]


class ShowrunnerState(NamedTuple):
    units: tuple[UnitJudgment, ...]
    merges_held: tuple[str, ...]
    outstanding: KeepOutstanding | ReplaceOutstanding


class RegistrationFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def command(step: str, arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RegistrationFailure(step, result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def notifier(arguments: list[str]) -> str:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    return command("notifier", ["zsh", str(script), *arguments])


def instance(name: str) -> InstancePresent | InstanceAbsent:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    result = subprocess.run(["zsh", str(script), "status", name], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return InstancePresent(result.stdout.strip())
    if "no instance" in result.stdout + result.stderr or "no such instance" in result.stdout + result.stderr:
        return InstanceAbsent()
    raise RegistrationFailure("notifier", result.stderr.strip() or result.stdout.strip() or f"status exit {result.returncode}")


class ScheduledUpdates(NamedTuple):
    minutes: int
    aligned: bool


class UpdatesOnDemand(NamedTuple):
    """The doc's Updates line reads `on demand`: a dailies runs only when the user asks for one."""


def update_schedule(lines: list[str]) -> ScheduledUpdates | UpdatesOnDemand:
    line = next((line for line in lines if line.startswith("- **Updates:**")), "")
    if re.search(r"\bon demand\b", line):
        return UpdatesOnDemand()
    match = re.search(r"every (\d+) minutes", line)
    minutes = int(match.group(1)) if match else 15
    if minutes <= 0:
        raise RegistrationFailure("prompt", "Updates interval must be positive")
    return ScheduledUpdates(minutes, "on the hour" in line)


def scheduled_prompt(minutes: int, zone: str, doc: Path) -> str:
    source = Path(__file__).resolve().parents[2] / "commands/showrunner/produce.md"
    document = source.read_text(encoding="utf-8")
    if "The prompt:\n" not in document:
        raise RegistrationFailure("register", "The prompt: marker is missing")
    tail = document.split("The prompt:\n", 1)[1]
    if "\n**A tick**" not in tail:
        raise RegistrationFailure("register", "**A tick** marker is missing")
    template = tail.split("\n**A tick**", 1)[0]
    prompt = "\n".join(line.removeprefix("> ").removeprefix(">") for line in template.strip().splitlines())
    prompt = prompt.replace("<N>", str(minutes)).replace("<zone>", zone).replace("<production doc>", str(doc))
    return prompt


def register(production: Production) -> None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        raise RegistrationFailure("register", "CLAUDE_CODE_SESSION_ID is required")
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    schedule = update_schedule(lines)
    prompt = (scheduled_prompt(schedule.minutes, str(production.zone), production.doc)
              if isinstance(schedule, ScheduledUpdates) else "")
    relative = production.doc.relative_to(production.checkout)
    dirty = command("register", ["git", "status", "--porcelain", "--", str(relative)], cwd=production.checkout)
    if dirty:
        raise RegistrationFailure("register", "the production doc has uncommitted edits; commit or discard them first")
    # Nothing registers the showrunner by name: its update timer, made below, is its one record.
    state_root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    prompt_file = state_root / production.slug / "prompt.txt"
    updates = f"showrunner-{production.slug}"
    if isinstance(schedule, ScheduledUpdates):
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        if not prompt_file.exists() or prompt_file.read_text(encoding="utf-8") != prompt:
            _ = prompt_file.write_text(prompt, encoding="utf-8")
        check = f"zsh {Path.home()}/.claude/scripts/production/production_check.sh {production.doc}"
        _ = notifier(["new", updates, "--to", f"session:{session_id}", "--every", str(schedule.minutes),
                  "--prompt-file", str(prompt_file), "--from", f"showrunner-timer-{production.slug}",
                  "--check", check, *(["--aligned"] if schedule.aligned else [])])
    elif isinstance(instance(updates), InstancePresent):
        # A schedule from before the doc said on demand would keep sending the prompt.
        _ = notifier(["remove", updates])
    for name, script in (("stall-watch", "stall_watch.py"), ("tmux-names", "tmux_names.py")):
        if isinstance(instance(name), InstanceAbsent):
            run = f"{Path.home()}/.claude/scripts/lib/py {Path.home()}/.claude/scripts/production/{script}"
            _ = notifier(["new", name, "--every", "1", "--run", run])
    if isinstance(schedule, UpdatesOnDemand):
        report("register", "ok", f"{updates} on demand: no scheduled dailies")
        return
    status = instance(updates)
    if isinstance(status, InstanceAbsent):
        raise RegistrationFailure("register", f"{updates} missing after new")
    next_due = next((line for line in status.output.splitlines() if line.startswith("next_due=")), "")
    if not next_due:
        raise RegistrationFailure("register", f"{updates} status lacks next_due")
    report("register", "ok", f"{updates} {next_due}")


def production_time(production: Production, stamp: str) -> str:
    zone = production.zone
    if re.fullmatch(r"\d{1,2}:\d{2}", stamp):
        today = datetime.now(zone).date()
        hour, minute = (int(part) for part in stamp.split(":"))
        moment = datetime(today.year, today.month, today.day, hour, minute, tzinfo=zone)
    else:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return moment.strftime("%H:%M %Z")


def state_error(step: str, path: Path, key: str, expected: str) -> RegistrationFailure:
    return RegistrationFailure(step, f"{path}: {key} must be {expected}")


def judgment_state(path: Path | None, step: str) -> ShowrunnerState:
    if path is None:
        return ShowrunnerState((), (), KeepOutstanding())
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise state_error(step, path, "root", "an object")
    fields = cast(dict[str, object], value)
    for key in fields:
        if key not in {"units", "merges_held", "open_for_user"}:
            raise state_error(step, path, key, "a known key")
    raw_units = fields.get("units", [])
    if not isinstance(raw_units, list):
        raise state_error(step, path, "units", "a list")
    units: list[UnitJudgment] = []
    for index, raw in enumerate(cast(list[object], raw_units)):
        key = f"units[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        unit = cast(dict[str, object], raw)
        for field in unit:
            if field not in {"unit", "phase", "wait"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        for field in ("unit", "phase", "wait"):
            if field in unit and not isinstance(unit[field], str):
                raise state_error(step, path, f"{key}.{field}", "text")
        units.append(UnitJudgment(cast(str, unit.get("unit", "")),
                                  cast(str, unit.get("phase", "not stated")),
                                  cast(str, unit.get("wait", "not stated"))))
    raw_held = fields.get("merges_held", [])
    if not isinstance(raw_held, list) or not all(isinstance(item, str) for item in cast(list[object], raw_held)):
        raise state_error(step, path, "merges_held", "a list of text")
    outstanding: KeepOutstanding | ReplaceOutstanding = KeepOutstanding()
    if "open_for_user" in fields:
        raw_open = fields["open_for_user"]
        if not isinstance(raw_open, list):
            raise state_error(step, path, "open_for_user", "a list")
        items: list[OpenForUserItem] = []
        for index, raw in enumerate(cast(list[object], raw_open)):
            key = f"open_for_user[{index}]"
            if isinstance(raw, str):
                if not raw.strip() or "\n" in raw:
                    raise state_error(step, path, key, "one nonempty line of text")
                items.append(OpenForUserItem(raw, ""))
                continue
            if not isinstance(raw, dict):
                raise state_error(step, path, key, "text or an object")
            item = cast(dict[str, object], raw)
            for field in item:
                if field not in {"text", "after"}:
                    raise state_error(step, path, f"{key}.{field}", "a known key")
            if not isinstance(item.get("text"), str) or not cast(str, item["text"]).strip() or "\n" in cast(str, item["text"]):
                raise state_error(step, path, f"{key}.text", "one nonempty line of text")
            after = item.get("after")
            if not isinstance(after, str) or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", after) is None:
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM")
            try:
                _ = datetime.fromisoformat(after)
            except ValueError:
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM") from None
            items.append(OpenForUserItem(cast(str, item["text"]), after))
        outstanding = ReplaceOutstanding(tuple(items))
    return ShowrunnerState(tuple(units), tuple(cast(list[str], raw_held)), outstanding)


def outstanding_path(production: Production) -> Path:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    return root / "outstanding" / f"{production.slug}.json"


def state_block(production: Production, state: ShowrunnerState, timestamp: str) -> str:
    history = merge_branch_history(production.checkout, production.merge_branch)
    judgments = {unit.unit: unit for unit in state.units}
    unit_lines: list[str] = []
    for cells in unit_table(production.doc.read_text(encoding="utf-8").splitlines()):
        name = cell_value(cells.get("Unit", ""))
        judgment = judgments.get(name)
        merged = history.last_code_for_unit(name)
        last = "none" if isinstance(merged, NoMerge) else f"phase {merged.phase} ({merged.short})"
        phase = judgment.phase if judgment is not None else "not stated"
        wait = judgment.wait if judgment is not None else "not stated"
        unit_lines.append(f"{name}: {phase}; last merged {last}; waits on {wait}")
    held_lines = ", ".join(state.merges_held)
    open_lines = ", ".join(item.text for item in read_outstanding(outstanding_path(production)))
    return "\n".join([f"### STATE {timestamp}", *unit_lines, f"Merges accepted but held: {held_lines or 'none'}",
                      f"Open for the user: {open_lines or 'none'}", ""])


def append_log(production: Production, event: str, path: Path | None, before_compaction: bool) -> None:
    if "\n" in event or not event.strip():
        raise RegistrationFailure("log", "event must be one nonempty line")
    state = judgment_state(path, "log")
    timestamp = production_time(production, datetime.now(production.zone).isoformat())
    previous = production.log.read_text(encoding="utf-8") if production.log.exists() else ""
    count = sum(bool(re.match(r"^- \d\d:\d\d \S+: ", line)) for line in previous.splitlines()) + 1
    block = state_block(production, state, timestamp) if count % 10 == 0 or before_compaction else ""
    production.log.parent.mkdir(parents=True, exist_ok=True)
    with production.log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {timestamp}: {event}\n")
        if block:
            _ = output.write(block)
    report("log", "ok", f"event {count}" + (" with STATE" if block else ""))


def footer(production: Production, path: Path | None, nothing_needed: bool) -> None:
    if footer_state(production.slug) is FooterState.OFF:
        report("footer", "ok", "footers off")
        return
    state = judgment_state(path, "footer")
    outstanding = outstanding_path(production)
    if isinstance(state.outstanding, ReplaceOutstanding):
        previous: dict[str, OutstandingItem] = {}
        if outstanding.exists():
            saved = cast(object, json.loads(outstanding.read_text(encoding="utf-8")))
            if isinstance(saved, list):
                for value in cast(list[object], saved):
                    if isinstance(value, dict):
                        fields = cast(dict[str, object], value)
                        text = fields.get("text")
                        since = fields.get("since")
                        if isinstance(text, str) and isinstance(since, str):
                            previous[text] = OutstandingItem(since=since, text=text,
                                                             after=str(fields.get("after", "")))
        since = datetime.now(production.zone).strftime("%Y-%m-%dT%H:%M")
        entries: list[dict[str, str]] = []
        for item in state.outstanding.items:
            entry = {"since": previous[item.text]["since"] if item.text in previous else since,
                     "text": item.text}
            if item.after:
                entry["after"] = item.after
            entries.append(entry)
        outstanding.parent.mkdir(parents=True, exist_ok=True)
        _ = outstanding.write_text(json.dumps(entries) + "\n", encoding="utf-8")
    schedule = instance(f"showrunner-{production.slug}")
    next_run: str | None = None
    if isinstance(schedule, InstancePresent) and not schedule.output.splitlines()[0].endswith(", stopped"):
        match = re.search(r"(?m)^next_due=(\d+)", schedule.output)
        if match is not None:
            due = datetime.fromtimestamp(int(match.group(1)), production.zone)
            days = (due.date() - datetime.now(production.zone).date()).days
            next_run = due.strftime("%H:%M") + (f"+{days}" if days > 0 else "")
    if footer_main(str(production.zone), next_run, None, outstanding, nothing_needed=nothing_needed):
        raise RegistrationFailure("footer", "renderer refused the footer")
    report("footer", "ok", "rendered")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("register", "time", "log", "footer"):
        subcommand = commands.add_parser(action)
        _ = subcommand.add_argument("--production", type=Path, required=True)
        if action == "register":
            # Accepted and unused: a showrunner started before its name was looked up still passes it.
            _ = subcommand.add_argument("--session")
        elif action == "time":
            _ = subcommand.add_argument("stamp")
        elif action == "log":
            _ = subcommand.add_argument("event")
            _ = subcommand.add_argument("--state", type=Path)
            _ = subcommand.add_argument("--before-compaction", action="store_true")
        else:
            _ = subcommand.add_argument("--state", type=Path)
            _ = subcommand.add_argument("--nothing-needed", action="store_true")
    args = parser.parse_args()
    action = cast(str, args.action)
    try:
        production = read_production(cast(Path, args.production))
        if action == "register":
            register(production)
        elif action == "time":
            report("time", "ok", production_time(production, cast(str, args.stamp)))
        elif action == "log":
            append_log(production, cast(str, args.event), cast(Path | None, args.state), cast(bool, args.before_compaction))
        else:
            footer(production, cast(Path | None, args.state), cast(bool, args.nothing_needed))
        return 0
    except (RegistrationFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, RegistrationFailure) else action
        report(step, "failed", str(error).replace("\n", "; "))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
