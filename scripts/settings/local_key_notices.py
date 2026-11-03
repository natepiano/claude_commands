"""Record local settings keys and deliver notices outside Git."""
from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

NoticeStatus = Literal["pending", "accepted"]
NoticeHistory = dict[str, dict[str, NoticeStatus]]
LEDGER = "settings.local-key-notices.json"


def read_history(path: Path) -> NoticeHistory:
    """Reject invalid history instead of losing accepted notices."""
    if not path.exists():
        return {}
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError("invalid notice history")
    history: NoticeHistory = {}
    for machine, entries in cast(dict[str, object], value).items():
        if not isinstance(entries, dict):
            raise ValueError("invalid machine notice history")
        notices: dict[str, NoticeStatus] = {}
        for key, status in cast(dict[str, object], entries).items():
            if status not in ("pending", "accepted"):
                raise ValueError("invalid notice status")
            notices[key] = status
        history[machine] = notices
    return history


def save_history(path: Path, history: NoticeHistory) -> None:
    write_text_atomic(path, json.dumps(history, sort_keys=True) + "\n")


def record(root: Path, machine: str, keys: list[str]) -> None:
    """Save new keys without contacting the message service."""
    if not keys:
        return
    path = root / LEDGER
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = read_history(path)
        notices = history.setdefault(machine, {})
        new = set(keys) - notices.keys()
        if new:
            notices.update(dict.fromkeys(new, "pending"))
            save_history(path, history)


def deliver(root: Path, machine: str) -> None:
    """Retry pending notices with the same sender identity."""
    # The clean filter does not import the sender or resolve its configuration.
    from scripts.message.send import TIMEOUT, OutcomeHeader, parse_outcome

    path = root / LEDGER
    if not path.exists():
        return
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = read_history(path)
        notices = history.get(machine, {})
        pending = [key for key, status in notices.items() if status == "pending"]
    if not pending:
        return
    destination = conf.read("settings").text("local_key_notice_to")
    for key in pending:
        identity = hashlib.sha256(json.dumps([machine, key]).encode()).hexdigest()
        command = [str(root / "scripts/lib/py"), str(root / "scripts/message/send.py"),
                   "--to", destination, "--from", "settings", "--key", "settings-local:" + identity,
                   "--repeat-minutes", str(timedelta.max.days * 1440),
                   "--text", f"Settings key {key} is local on {machine}."]
        accepted = False
        try:
            sent = subprocess.run(command, input="", text=True, capture_output=True,
                                  check=False, timeout=TIMEOUT + 40)
            outcome = parse_outcome(sent.stdout)
            accepted = isinstance(outcome, OutcomeHeader) and outcome.outcome in {"sent", "skipped", "queued"}
        except (OSError, subprocess.TimeoutExpired):
            pass
        if accepted:
            with path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                history = read_history(path)
                history.setdefault(machine, {})[key] = "accepted"
                save_history(path, history)
        else:
            print(f"settings: local key notice failed for {key}; retry pending", file=sys.stderr)


def main(arguments: list[str]) -> int:
    """Run one notice operation for a settings repository."""
    try:
        mode, folder, machine, encoded = arguments
        if mode == "record":
            keys = cast(object, json.loads(encoded))
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in cast(list[object], keys)):
                raise ValueError("invalid notice keys")
            record(Path(folder), machine, cast(list[str], keys))
        elif mode == "deliver":
            deliver(Path(folder), machine)
        else:
            raise ValueError("invalid notice operation")
    except (OSError, ValueError, conf.Unusable) as error:
        print(f"settings: local key notices: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
