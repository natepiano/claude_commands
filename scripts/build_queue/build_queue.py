#!/usr/bin/env python3
"""Show the build queue, or one step's place and why it waits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import Span, age as age, json_form, plain
from scripts.build_queue.queue_view import QueueModel, field as field, render_view, step_labels as step_labels
from scripts.lint import memory_admit
from scripts.lib.user_zone import user_zone
from scripts.message import unit_labels


def caller() -> str:
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


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

    if len(sys.argv) > 1 and sys.argv[1] == "bump":
        if len(sys.argv) != 3 or len(sys.argv[2].split()) != 1:
            print("build queue: use /build:queue bump <unit>")
            return 3
        try:
            memory_admit.run_priority(["set", "--unit", sys.argv[2], "--why", "the user's bump"])
        except SystemExit as stopped:
            return stopped.code if isinstance(stopped.code, int) else 1
        except (OSError, ValueError) as error:
            print(f"build queue: {error}", file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true")
    _ = parser.add_argument("--columns", type=int)
    _ = parser.add_argument("--you")
    _ = parser.add_argument("words", nargs="*")
    class Arguments(argparse.Namespace):
        you: str = caller()
        json: bool = False
        columns: int = default_columns()
        words: list[str] = []
    arguments = parser.parse_args(namespace=Arguments())
    if arguments.columns < 1:
        parser.error("--columns must be positive")
    words = arguments.words
    if len(words) > 1:
        parser.error("use one selector")
    try:
        reading = memory_admit.queue_read()
        if isinstance(reading, memory_admit.QueueBusy):
            if arguments.json:
                print(json.dumps(json_form([(Span("queue busy; try again"),)], arguments.columns, 0)))
            else:
                print("queue busy; try again")
            return 0
        facts = head_facts.read_head_facts(reading) if words else memory_admit.NoWaiter()
        ids = [step.session_id for step in (*reading.view.running, *reading.view.waiting)]
        ids.extend(head_facts.session_ids(facts))
        labels = unit_labels.unit_labels_for(ids, os.environ)
        pace = head_facts.read_pace(reading.explanation.now) if words else head_facts.PaceUnavailable()
        model = QueueModel(reading, labels, facts, pace, user_zone())
        if words:
            from scripts.build_queue.queue_card import render_selection

            lines = render_selection(model, words[0], arguments.columns)
        else:
            lines = render_view(model, arguments.columns, colour=arguments.json, you=arguments.you)
        print(json.dumps(json_form(lines, arguments.columns, reading.explanation.now),
                         ensure_ascii=False, separators=(",", ":")) if arguments.json else plain(lines))
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
