#!/usr/bin/env python3
"""Show the build queue, or one step's place and why it waits."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import age as age, json_form, plain
from scripts.build_queue.queue_view import QueueModel, field as field, render_view, shown_waiters, step_labels as step_labels
from scripts.lint import memory_admit
from scripts.lib.user_zone import user_zone
from scripts.message import unit_labels


def bump_selector(selector: str, reading: memory_admit.QueueRead,
                  labels: Mapping[str, unit_labels.UnitLabels]) -> str | memory_admit.WaiterRowSelector:
    matches = shown_waiters(selector, reading, labels)
    if len(matches) > 1:
        descriptions = ", ".join(f"{position}: {step_labels(reading.view.waiting[position - 1], labels).session} "
                                 + reading.view.waiting[position - 1].folder for position in matches)
        raise memory_admit.UnmatchedWaiter(f"{selector!r}: ambiguous waiter; matches: {descriptions}")
    if matches:
        position = matches[0] - 1
        return memory_admit.WaiterRowSelector(reading.ordered[position][0].name, selector)
    return selector


def caller() -> str:
    return (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or f"user:{os.environ.get('USER', 'unknown')}")


def refusal_text(refusal: memory_admit.UnmatchedWaiter) -> str:
    escaped = "".join(character if character.isprintable() else json.dumps(character)[1:-1]
                      for character in str(refusal))
    return f"build queue: {escaped}"


def default_columns() -> int:
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = 0
    if columns > 0:
        return columns
    if sys.stdout.isatty():
        try:
            columns = os.get_terminal_size(sys.stdout.fileno()).columns
            if columns > 0:
                return columns
        except OSError:
            pass
    return 100


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true")
    _ = parser.add_argument("--columns", type=int)
    _ = parser.add_argument("words", nargs="*")
    class Arguments(argparse.Namespace):
        json: bool = False
        columns: int = default_columns()
        words: list[str] = []
    arguments = parser.parse_args(namespace=Arguments())
    if arguments.columns < 1:
        parser.error("--columns must be positive")
    words = arguments.words
    if len(words) > 1 and (len(words) != 2 or words[0] != "bump"):
        parser.error("use one selector, or bump <selector>")
    if words == ["bump"]:
        parser.error("bump needs a selector")
    try:
        reading = memory_admit.locked_queue_read()
        facts = head_facts.read_head_facts(reading)
        ids = [step.session_id for step in (*reading.view.running, *reading.view.waiting)]
        ids.extend(head_facts.session_ids(facts))
        labels = unit_labels.unit_labels_for(ids, os.environ)
        if words and words[0] == "bump":
            actor = caller()
            try:
                moved = memory_admit.bump_waiter(bump_selector(words[1], reading, labels), actor)
            except memory_admit.UnmatchedWaiter as error:
                print(refusal_text(error))
                return 3
            print(f"{field(actor)} moved {step_labels(moved, labels).session}: "
                  + f"{field(moved.step)} in {field(moved.folder)} to the head; memory limits still apply")
            return 0
        model = QueueModel(reading, labels, facts, head_facts.read_pace(reading.explanation.now), user_zone())
        if words:
            from scripts.build_queue.queue_card import render_selection

            lines = render_selection(model, words[0], arguments.columns)
        else:
            lines = render_view(model, arguments.columns)
        print(json.dumps(json_form(lines, arguments.columns, reading.explanation.now),
                         ensure_ascii=False, separators=(",", ":")) if arguments.json else plain(lines))
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
