#!/usr/bin/env python3
"""Set every running unit director's effort, and the effort new unit directors launch with.

Usage: unit_effort.py <level> [--check]

Every unit director on this machine, whatever its production, is found by its host's marks
(`unit_lookup.every_marked_unit`, the enumeration the account swap reads too). Each gets
`/effort <level>` through the host's typing interface, never C-c or Escape; typing
while it is mid-turn is fine. A pane showing a form, prompt or menu, or a draft in its input line,
is skipped and reported. The registry's `production.director` row keeps its model and takes the
level, through agent_admin.sh and its shared-registry publisher, before anything is typed. `--check` sends nothing and changes nothing.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import unit_lookup
from session_pane import HerdrFailure, read_place, type_place

AGENTS = Path(__file__).resolve().parents[1] / "agents"
ROW = "production.director"
USAGE = "usage: unit_effort.py <level> [--check]"
READ_ROW = ('source "$1" && agents_resolve production.director && printf "%s\\n%s\\n%s\\n" '
            + '"$AGENT_MODEL" "$AGENT_EFFORT" "$(_agents_registry_get "$AGENT_FAMILY.agents" "$AGENT_MODEL")"')
# How many lines up from the bottom a form is looked for when no input box is on screen.
BOTTOM_LINES = 25
SGR = re.compile(r"\x1b\[([0-9;:]*)m")
CHOICE = re.compile(r"❯\s*\d+\.")
FORM_TEXT = ("Do you want", "Esc to cancel", "Enter to confirm", "Enter to select")
PROMPT = "❯"
RULE = "──"


class DirectorRow(NamedTuple):
    model: str
    effort: str
    # The efforts the registry lets this model take.
    allowed: list[str]


class Ready(NamedTuple):
    pass


class Skipped(NamedTuple):
    reason: str


class Cell(NamedTuple):
    char: str
    dim: bool
    inverse: bool


def director_row() -> DirectorRow:
    """The unit director launch row now. Raises OSError when the registry cannot resolve it."""
    done = subprocess.run(["bash", "-c", READ_ROW, "_", str(AGENTS / "agents_config.sh")],
                          capture_output=True, text=True, check=False)
    values = done.stdout.splitlines()
    if done.returncode != 0 or len(values) != 3 or not values[0]:
        errors = [line for line in done.stderr.splitlines() if line.startswith("ERROR:")]
        raise OSError(errors[0] if errors else f"cannot resolve {ROW}")
    model, effort, allowed = values
    return DirectorRow(model, effort, [item.strip() for item in allowed.split(",") if item.strip()])


def set_row(model: str, level: str) -> str:
    """Publish the shared launch row at `<model>:<level>`; empty on success, else why not."""
    done = subprocess.run(["bash", str(AGENTS / "agent_admin.sh"), ROW, f"{model}:{level}"],
                          capture_output=True, text=True, check=False)
    if done.returncode == 0:
        return ""
    errors = [line for line in done.stderr.splitlines() if line.startswith("ERROR:")]
    return errors[0] if errors else f"agent_admin.sh exit {done.returncode}"


def _sgr(parameters: str, dim: bool, inverse: bool) -> tuple[bool, bool]:
    """Dim and inverse after one SGR sequence. A colour's own numbers are skipped, never read as codes."""
    codes = parameters.split(";")
    index = 0
    while index < len(codes):
        code = codes[index]
        if code in ("38", "48", "58") and index + 1 < len(codes):
            index += 3 if codes[index + 1] == "5" else 5 if codes[index + 1] == "2" else 1
            continue
        if code in ("", "0"):
            dim = inverse = False
        dim = True if code == "2" else False if code == "22" else dim
        inverse = True if code == "7" else False if code == "27" else inverse
        index += 1
    return dim, inverse


def cells(line: str) -> list[Cell]:
    """Each character of a `capture-pane -e` line with whether it shows dim and whether inverse."""
    found: list[Cell] = []
    dim = inverse = False
    position = 0
    for match in SGR.finditer(line):
        found.extend(Cell(char, dim, inverse) for char in line[position:match.start()])
        dim, inverse = _sgr(match.group(1), dim, inverse)
        position = match.end()
    found.extend(Cell(char, dim, inverse) for char in line[position:])
    return found


def _input_box(plain: list[str]) -> tuple[int, int] | None:
    """The first and past-the-last line of the input box: from the `❯` line under a rule to the next rule."""
    starts = [index for index in range(1, len(plain))
              if plain[index].startswith(PROMPT) and plain[index - 1].lstrip().startswith(RULE)]
    if not starts:
        return None
    start = starts[-1]
    end = next((index for index in range(start + 1, len(plain)) if plain[index].lstrip().startswith(RULE)),
               len(plain))
    return start, end


def _holds_draft(styled: list[str]) -> bool:
    """Whether the input box holds typed text. A suggestion shows dim, save the cursor's one inverse
    cell, and typing replaces it; an empty input holds a non-breaking space after the `❯`."""
    after = [cell for line in styled for cell in cells(line)]
    mark = next((index for index, cell in enumerate(after) if cell.char == PROMPT), -1)
    shown = [cell for cell in after[mark + 1:] if not cell.char.isspace()]
    if not shown:
        return False
    ghost = any(cell.dim for cell in shown) and all(cell.dim or cell.inverse for cell in shown)
    return not ghost


def verdict(capture: str) -> Ready | Skipped:
    """Whether `/effort` may be typed into a pane whose `capture-pane -p -e` text this is."""
    styled = capture.splitlines()
    plain = [SGR.sub("", line) for line in styled]
    box = _input_box(plain)
    region = plain[box[0]:] if box is not None else plain[-BOTTOM_LINES:]
    if any(CHOICE.search(line) or any(text in line for text in FORM_TEXT) for line in region):
        return Skipped("a form, prompt or menu is open")
    if box is None:
        return Skipped("no input line on screen")
    if _holds_draft(styled[box[0]:box[1]]):
        return Skipped("a draft is in its input line")
    return Ready()


def send(place: unit_lookup.Place, level: str) -> str:
    """Type `/effort <level>` into the pane, then Enter; the empty string when both took, else why not."""
    try:
        type_place(place, f"/effort {level}")
    except (OSError, HerdrFailure) as error:
        return str(error)
    return ""


def _director_line(marked: unit_lookup.MarkedUnit, level: str, check: bool) -> str:
    claude = marked.claude
    if isinstance(claude, unit_lookup.ClaudeUnknown):
        return f"{marked.unit}: skipped — cannot tell whether Claude runs: {claude.reason}"
    name = claude.name if isinstance(claude, unit_lookup.LiveClaude) and claude.name else marked.unit
    try:
        captured = read_place(marked.place, ansi=True)
    except (OSError, HerdrFailure) as error:
        return f"{name}: skipped — its pane could not be read: {error}"
    judged = verdict(captured)
    if isinstance(judged, Skipped):
        return f"{name}: skipped — {judged.reason}"
    if check:
        return f"{name}: would send"
    problem = send(marked.place, level)
    return f"{name}: NOT sent — {problem}" if problem else f"{name}: sent"


def director_lines(every: unit_lookup.MarkedProductions, level: str, check: bool) -> list[str]:
    """One line per running unit director, every production's, then each production left out."""
    lines = [_director_line(marked, level, check)
             for slug in sorted(every.units) for _, marked in sorted(every.units[slug].items())
             if not isinstance(marked.claude, unit_lookup.ClaudeNotRunning)]
    lines.extend(f"{slug}: skipped {fault}" for slug, units in sorted(every.faults.items())
                 for _, fault in sorted(units.items()))
    return lines or ["no unit director is running"]


def registry_line(row: DirectorRow, level: str, check: bool) -> str:
    wanted = f"{row.model}:{level}"
    now = f"{row.model}:{row.effort}" if row.effort else row.model
    if row.effort == level:
        return f"registry: {ROW} is already {wanted}"
    return f"registry: {ROW} would be {wanted} (now {now})" if check else f"registry: {ROW} is now {wanted} (was {now})"


def run(level: str, check: bool) -> int:
    """Exit 0 when every director that could take it was sent it, 1 when a send or the registry failed."""
    try:
        row = director_row()
    except OSError as error:
        print(f"unit_effort: {error}", file=sys.stderr)
        return 1
    if level not in row.allowed:
        print(f"unit_effort: {row.model} takes {', '.join(row.allowed) or 'no effort'}, not {level}", file=sys.stderr)
        return 2
    try:
        every = unit_lookup.every_marked_unit()
    except OSError as error:
        print(f"unit_effort: {error}", file=sys.stderr)
        return 1
    problem = "" if check or row.effort == level else set_row(row.model, level)
    if problem:
        print(f"unit_effort: nothing sent; {ROW} was not changed: {problem}", file=sys.stderr)
        return 1
    lines = [*director_lines(every, level, check), registry_line(row, level, check)]
    print("\n".join(lines))
    return 1 if any(": NOT sent — " in line for line in lines) else 0


def main(arguments: list[str]) -> int:
    match arguments:
        case [level] if not level.startswith("-"):
            return run(level, False)
        case [level, "--check"] | ["--check", level] if not level.startswith("-"):
            return run(level, True)
        case _:
            print(USAGE, file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
