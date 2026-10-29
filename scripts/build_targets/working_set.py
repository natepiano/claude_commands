#!/usr/bin/env python3
"""Show resident PSS and swap by process kind from /proc.

Ownership is loaded once from the adjacent working_set_owners.json snapshot.
Regenerate it when production Units / Owns cells change. The command writes
nothing. Unknown ownership is printed as unassigned.
PSS shares resident pages between the processes that map them. It excludes
kernel memory and unmapped cache, so it is not the machine's used memory.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class Category:
    name: str
    programs: tuple[str, ...]


# First match wins; inspect the program (or a node entry point), never payload text.
CATEGORIES = (
    Category("cargo/rustc", ("cargo", "rustc", "rustdoc", "cargo-*")),
    Category("Claude sessions", ("claude", "claude-code", "claude-internal")),
    Category("Codex", ("codex", "codex-*")),
    Category("Python helpers", ("python", "python[0-9]*", "pypy*")),
    Category("other", ("*",)),
)

def load_owners(path: Path) -> tuple[tuple[str, str], ...]:
    """Validate the generated snapshot before using any ownership rules."""
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(data, dict):
            raise ValueError("expected an object with generated_on, sources and rules")
        document = cast(dict[str, object], data)
        generated_on, sources, rows = (document.get(key) for key in ("generated_on", "sources", "rules"))
        if not isinstance(generated_on, str):
            raise ValueError("generated_on must be an ISO date")
        _ = date.fromisoformat(generated_on)
        if not isinstance(sources, list) or not sources:
            raise ValueError("sources must be a nonempty list of document paths")
        if any(not isinstance(source, str) or not source for source in cast(list[object], sources)):
            raise ValueError("sources must contain nonempty document paths")
        if not isinstance(rows, list) or not rows:
            raise ValueError("rules must be a nonempty list")
        rules: list[tuple[str, str]] = []
        for row in cast(list[object], rows):
            if not isinstance(row, dict):
                raise ValueError("each rule must have a path and owner")
            rule = cast(dict[str, object], row)
            pattern, owner = rule.get("path"), rule.get("owner")
            if not isinstance(pattern, str) or not pattern.startswith("scripts/"):
                raise ValueError("rule path must start with scripts/")
            if not isinstance(owner, str) or not owner.strip():
                raise ValueError("rule owner must be a nonempty string")
            rules.append((pattern, owner))
        return tuple(rules)
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot load ownership snapshot {path}: {error}") from error


try:
    OWNERS = load_owners(Path(__file__).with_name("working_set_owners.json"))
except ValueError as error:
    if __name__ != "__main__":
        raise
    print(f"working-set: {error}", file=sys.stderr)
    sys.exit(1)


# Node options whose value can be the next argument. Equals forms need no skip.
NODE_VALUE_OPTIONS = frozenset((
    "-r", "--require", "--import", "--loader", "--experimental-loader",
    "--inspect-port", "--max-old-space-size", "--max-semi-space-size",
    "--stack-size", "--stack_size", "--conditions", "-C", "--input-type",
    "--title", "--env-file", "--env-file-if-exists",
))


def node_entry(arguments: tuple[str, ...]) -> str:
    """Return only the entry script, never preload modules or inline source."""
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if argument == "-" or not argument.startswith("-"):
            break
        option = argument.split("=", 1)[0]
        if option in ("--eval", "--print") or argument.startswith(("-e", "-p")) and not argument.startswith("--"):
            return ""
        index += 2 if argument in NODE_VALUE_OPTIONS else 1
    return arguments[index] if index < len(arguments) else ""



def classify(arguments: tuple[str, ...]) -> str:
    program = os.path.basename(arguments[0]) if arguments else ""
    if program in ("node", "nodejs", "bun") and len(arguments) > 1:
        entry = node_entry(arguments) if program in ("node", "nodejs") else arguments[1]
        if "/@anthropic-ai/claude-code/" in entry:
            program = "claude"
        elif "/@openai/codex/" in entry:
            program = "codex"
    for category in CATEGORIES:
        if any(fnmatch.fnmatchcase(program, pattern) for pattern in category.programs):
            return category.name
    return "other"


def python_script(arguments: tuple[str, ...], cwd: str) -> str:
    """Consume interpreter options before naming the script, module or inline code."""
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if argument == "-" or not argument.startswith("-"):
            break
        if argument == "--check-hash-based-pycs":
            index += 2
            continue
        # -W and -X take the rest of their argument, or the next argument.
        # -c and -m do the same and end interpreter option parsing.
        for offset, option in enumerate(argument[1:], start=1):
            if option not in "cmWX":
                continue
            if option == "c":
                return "<inline>"
            value = argument[offset + 1:]
            if not value:
                index += 1
                value = arguments[index] if index < len(arguments) else "<missing>"
            if option == "m":
                return "module:" + value
            break
        index += 1
    if index >= len(arguments):
        return "<interactive>"
    script = arguments[index]
    if script == "-":
        return "<stdin>"
    return os.path.normpath(script if os.path.isabs(script) else os.path.join(cwd, script))


def owner_for(script: str) -> str:
    # Normalize checkout prefixes without following links outside /proc.
    relative = "scripts/" + script.split("/scripts/", 1)[1] if "/scripts/" in script else script
    for path, owner in OWNERS:
        if fnmatch.fnmatchcase(relative, path) or (path.endswith("/") and relative.startswith(path)):
            return owner
    return "unassigned"


@dataclass(frozen=True)
class ProcessMemory:
    pid: int
    command: str
    pss_kib: int
    swap_kib: int


@dataclass
class MemoryGroup:
    category: str
    script: str = ""
    owner: str = ""
    count: int = 0
    pss_kib: int = 0
    swap_kib: int = 0
    top: list[ProcessMemory] = field(default_factory=list)

    def add(self, process: ProcessMemory) -> None:
        self.count += 1
        self.pss_kib += process.pss_kib
        self.swap_kib += process.swap_kib
        self.top.append(process)
        self.top.sort(key=lambda item: (item.pss_kib, item.swap_kib), reverse=True)
        del self.top[3:]


@dataclass(frozen=True)
class MachineMemory:
    total_kib: int
    available_kib: int
    swap_total_kib: int
    swap_used_kib: int


@dataclass(frozen=True)
class WorkingSet:
    groups: tuple[MemoryGroup, ...]
    machine: MachineMemory
    unreadable: int


def memory_fields(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            key, separator, rest = line.partition(":")
            parts = rest.split()
            if separator and parts and parts[0].isdigit():
                values[key] = int(parts[0])
    return values


PROC = Path("/proc")


def collect(proc: Path = PROC) -> WorkingSet:
    memory = memory_fields(proc / "meminfo")
    machine = MachineMemory(memory["MemTotal"], memory["MemAvailable"], memory["SwapTotal"],
                            memory["SwapTotal"] - memory["SwapFree"])
    categories = {category.name: MemoryGroup(category.name) for category in CATEGORIES}
    helpers: dict[str, MemoryGroup] = {}
    unreadable = 0
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            process = Path(entry.path)
            try:
                command_line = (process / "cmdline").read_bytes().decode(errors="replace")
                parts = command_line.split("\0")
                if parts[-1] == "":
                    _ = parts.pop()
                arguments = tuple(parts)
                if not arguments:
                    # Kernel threads and zombies have no userspace working set.
                    continue
                values = memory_fields(process / "smaps_rollup")
                pss, swap = values["Pss"], values["Swap"]
            except (OSError, KeyError, ValueError):
                unreadable += 1
                continue
            category = classify(arguments)
            command = os.path.basename(arguments[0])
            sample = ProcessMemory(int(entry.name), command, pss, swap)
            categories[category].add(sample)
            if category == "Python helpers":
                try:
                    cwd = os.readlink(process / "cwd")
                except OSError:
                    cwd = "<cwd unreadable>"
                script = python_script(arguments, cwd)
                group = helpers.setdefault(script, MemoryGroup(category, script, owner_for(script)))
                group.add(sample)
    groups: list[MemoryGroup] = []
    for category in CATEGORIES:
        groups.append(categories[category.name])
        if category.name == "Python helpers":
            groups.extend(sorted(helpers.values(), key=lambda group: group.pss_kib, reverse=True))
    return WorkingSet(tuple(groups), machine, unreadable)


def render(report: WorkingSet) -> str:
    lines = ["Memory in MiB; PSS is resident shared memory divided between its users.",
             f"{'Category / script [owner]':76} {'Count':>5} {'PSS':>10} {'Swap':>10}"]
    for group in report.groups:
        label = f"  {group.script} [{group.owner}]" if group.script else group.category
        # Escape control characters from process-controlled names before terminal output.
        label = label.encode("unicode_escape").decode("ascii")
        lines.append(f"{label:76} {group.count:5d} {group.pss_kib / 1024:10.1f} {group.swap_kib / 1024:10.1f}")
        if group.top:
            top = "; ".join(f"{item.pid} {item.command.encode('unicode_escape').decode('ascii')} "
                            + f"({item.pss_kib / 1024:.1f} PSS, {item.swap_kib / 1024:.1f} swap)" for item in group.top)
            lines.append("    Top: " + top)
    machine = report.machine
    lines.append(f"Machine: {machine.total_kib / 1024:.1f} MiB total; {machine.available_kib / 1024:.1f} MiB available; "
                 + f"swap {machine.swap_used_kib / 1024:.1f} / {machine.swap_total_kib / 1024:.1f} MiB used.")
    lines.append(f"Unreadable or exited processes: {report.unreadable}. Python detail rows are part of the Python total.")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 1:
        print("usage: working-set", file=sys.stderr)
        return 2
    try:
        print(render(collect()))
    except (OSError, KeyError, ValueError) as error:
        print(f"working-set: cannot read Linux /proc: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
