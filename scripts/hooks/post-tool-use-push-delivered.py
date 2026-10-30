#!/usr/bin/env python3
"""Consume fallback approval only after the matching push succeeds."""
from __future__ import annotations

import fcntl
import json
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ._push_fallback import fallback_matches, marker_path, object_record, text
else:
    from _push_fallback import fallback_matches, marker_path, object_record, text


def main() -> None:
    try:
        payload = object_record(cast(object, json.load(sys.stdin)))
        if text(payload, "tool_name") != "PushNotification":
            return
        response = object_record(payload.get("tool_response"))
        if response.get("success") is not True or response.get("is_error") is True:
            return
        marker = marker_path(text(payload, "session_id"))
        if not marker.parent.exists():
            return
        push = object_record(payload.get("tool_input"))
        with (marker.parent / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                record = object_record(cast(object, json.loads(marker.read_text(encoding="utf-8"))))
            except FileNotFoundError:
                return
            if fallback_matches(text(push, "message"), record):
                marker.unlink()
    except Exception as error:
        print(f"push delivered: {type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
