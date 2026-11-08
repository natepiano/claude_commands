"""Registered phone fixtures for tests that replace posting."""
from __future__ import annotations
from pathlib import Path
from typing import cast
from unittest import mock
from scripts.notify import pushover
from scripts.notify import push_texts
from scripts.notify.user_action import SessionLine, NO_SESSION_LINE


def registered_send(argv: list[str], keys: Path, post: pushover.Post) -> int:
    """Use a registered fixture for tests of review and channel behavior."""
    if "--text-id" in argv:
        return pushover.send(argv, keys, post)
    parsed = pushover.parse(argv)
    if parsed is None:
        return pushover.send(argv, keys, post)
    action = (push_texts.ActionTemplate("{action}") if parsed["action"] is not None
              else push_texts.NoActionNeeded())
    session = (push_texts.SessionTemplate("{session}") if isinstance(parsed["session"], SessionLine)
               else NO_SESSION_LINE)
    values = {"title": parsed["title"], "body": parsed["message"]}
    if parsed["action"] is not None:
        values["action"] = parsed["action"]
    if isinstance(parsed["session"], SessionLine):
        values["session"] = parsed["session"].text
    entry = push_texts.PushText("fixture.admitted", parsed["source"] or "python test_pushover.py",
        cast(push_texts.Need, parsed["need"]), "{title}", action, "{body}",
        (push_texts.PushExample("fixture", values),), purpose="Channel test fixture.", session=session, agent_written=True)
    options = ["--text-id", entry.id, "--priority", parsed["priority"],
               *(item for key, value in values.items() for item in ("--value", f"{key}={value}"))]
    if parsed["source"] is not None:
        options.extend(["--source", parsed["source"]])
    if parsed["due"]:
        options.extend(["--due", parsed["due"]])
    if parsed["key"]:
        options.extend(["--key", parsed["key"]])
    if parsed["answerable"]:
        options.append("--answerable")
    entries = push_texts.catalog()
    with mock.patch.object(push_texts, "catalog", return_value=(*entries, entry)):
        return pushover.send(options, keys, post)
