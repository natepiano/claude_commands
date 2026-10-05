#!/usr/bin/env python3
"""Send the user one Pushover alert: the one way any session reaches their phone.

  pushover.py [--priority 0|1|2] TITLE MESSAGE

Priority 0 is a normal notification, 1 high (sounds in Pushover's quiet hours),
2 emergency: it repeats every RETRY_S seconds until the user taps Acknowledge,
for at most EXPIRE_S. Which events get which priority is each caller's policy.

The keys are in KEYS_FILE (PUSHOVER_USER, PUSHOVER_TOKEN; 0600, filled in by the
user). Nothing here prints them. (user, 2026-10-05) Each LOG_FILE JSON line
holds the local time, priority, posted title and message, and outcome.

Exit 0 sent, 1 Pushover refused it or could not be reached, 2 usage or keys
missing.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

API_URL = "https://api.pushover.net/1/messages.json"
KEYS_FILE = Path.home() / ".config" / "pushover" / "env"
LOG_FILE = Path.home() / ".local" / "state" / "notify" / "pushover.jsonl"
PRIORITIES = ("0", "1", "2")
RETRY_S = 300
EXPIRE_S = 10800
TITLE_MAX = 250
MESSAGE_MAX = 1024
TIMEOUT_S = 15
USAGE = "usage: pushover.py [--priority 0|1|2] TITLE MESSAGE"


class Keys(TypedDict):
    user: str
    token: str


class Reply(TypedDict, total=False):
    status: int
    request: str
    receipt: str
    errors: list[str]


Post = Callable[[str, bytes], tuple[int, bytes]]


def read_keys(path: Path) -> Keys | None:
    """Both keys from a KEY=VALUE file, or None when either is missing or empty."""
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.lstrip().startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    user, token = values.get("PUSHOVER_USER", ""), values.get("PUSHOVER_TOKEN", "")
    return {"user": user, "token": token} if user and token else None


def fields(keys: Keys, title: str, message: str, priority: str) -> dict[str, str]:
    body = {
        "token": keys["token"],
        "user": keys["user"],
        "title": title[:TITLE_MAX],
        "message": message[:MESSAGE_MAX],
        "priority": priority,
    }
    if priority == "2":
        body |= {"retry": str(RETRY_S), "expire": str(EXPIRE_S)}
    return body


def http_post(url: str, data: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # pyright: ignore[reportAny]
            return cast(int, response.status), cast(bytes, response.read())  # pyright: ignore[reportAny]
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def log(priority: str, title: str, message: str, outcome: str) -> None:
    entry = {"time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
             "priority": priority, "title": title, "message": message, "outcome": outcome}
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            _ = out.write(json.dumps(entry) + "\n")
    except OSError as error:
        print(f"pushover: could not write log: {error}", file=sys.stderr)


def send(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    priority = "0"
    if argv[:1] == ["--priority"]:
        if len(argv) < 2 or argv[1] not in PRIORITIES:
            print(USAGE, file=sys.stderr)
            return 2
        priority, argv = argv[1], argv[2:]
    if len(argv) != 2 or not all(arg.strip() for arg in argv):
        print(USAGE, file=sys.stderr)
        return 2
    title, message = argv
    title, message = title[:TITLE_MAX], message[:MESSAGE_MAX]
    keys = read_keys(keys_file)
    if keys is None:
        log(priority, title, message, "keys missing")
        print(f"pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in {keys_file}", file=sys.stderr)
        return 2
    data = urllib.parse.urlencode(fields(keys, title, message, priority)).encode()
    try:
        status, raw = post(API_URL, data)
    except (urllib.error.URLError, TimeoutError) as error:
        log(priority, title, message, f"unreachable: {error}")
        print(f"pushover: could not reach Pushover: {error}", file=sys.stderr)
        return 1
    try:
        decoded = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        decoded = None
    reply = cast(Reply, cast(object, decoded)) if isinstance(decoded, dict) else Reply()
    if status == 200 and reply.get("status") == 1:
        receipt = reply.get("receipt")
        outcome = f"sent, receipt {receipt}" if receipt else "sent"
        log(priority, title, message, outcome)
        print(f"pushover: {outcome}")
        return 0
    errors = "; ".join(reply.get("errors", [])) or f"HTTP {status}"
    log(priority, title, message, f"refused: {errors}")
    print(f"pushover: refused: {errors}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(send(sys.argv[1:]))
