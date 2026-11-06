"""Keep one accepted alert for each unrouted row and reason."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from scripts.lib import atomic_file, conf, file_lock
from scripts.message import send_result, session_send


def send_message(recipient: str, sender: str, message: str) -> None:
    """Accept delivered messages and messages kept in the sender's queue."""
    result = subprocess.run(session_send.send_argv("--to", recipient, "--from", sender, "--text", message),
                            capture_output=True, text=True, check=False)
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        match outcome.header.outcome:
            case "sent" | "queued" | "kept":
                return
            case "retry":
                raise RuntimeError(f"RETRY: {outcome.header.detail}")
            case "skipped" | "replaced" | "failed" | "refused" | "held" | "pending":
                pass
    raise RuntimeError((result.stderr or result.stdout).strip() or "message delivery failed")


def notify(root: Path, listing: str, row: dict[str, str], reason: str, candidates: tuple[str, ...]) -> None:
    """Try one alert without waiting for another render's lock."""
    try:
        _notify(root, listing, row, reason, candidates)
    except (OSError, RuntimeError, subprocess.SubprocessError, conf.Unusable) as error:
        print("review-links unrouted alert: " + " ".join(str(error).splitlines()), file=sys.stderr)


def _notify(root: Path, listing: str, row: dict[str, str], reason: str, candidates: tuple[str, ...]) -> None:
    key = hashlib.sha256(json.dumps([listing, row["#"], reason]).encode()).hexdigest()
    folder = root / ".review-link-unrouted"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + ".json")
    with file_lock.exclusive(folder / (key + ".lock"), wait=False) as result:
        if result is file_lock.LockResult.BUSY:
            return
        saved: dict[str, object]
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
            saved = cast(dict[str, object], value) if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError):
            saved = {}
        if saved.get("sent") is True:
            return
        settings = conf.read("review_links")
        pause = settings.origin("unrouted_alert_retry_seconds", float)
        if isinstance(pause, conf.UnavailableValue):
            raise conf.Unusable(pause.reason)
        now = time.time()
        last = saved.get("last_try")
        if isinstance(last, (int, float)) and now - last < pause.value:
            return
        atomic_file.write_text_atomic(path, json.dumps({"last_try": now, "sent": False}) + "\n")
        message = "\n".join(("Change review routing needs attention", f"List: {listing}", f"Row: {row['#']}",
                              f"Change: {row.get('Change', '')}", f"Reason: {reason}",
                              "Candidates: " + ", ".join(candidates)))
        recipient = settings.origin("unrouted_alert_recipient", str)
        if isinstance(recipient, conf.UnavailableValue):
            raise conf.Unusable(recipient.reason)
        send_message(recipient.value, Path(__file__).stem, message)
        atomic_file.write_text_atomic(path, json.dumps({"last_try": now, "sent": True}) + "\n")
