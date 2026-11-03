"""Keep slash-command evidence until the prompt hook consumes its hash.

Each submission has one record without time expiry.
The configured count limit removes the oldest evidence and records its cause.
"""
from __future__ import annotations

import hashlib
import fcntl
import os
import json
import socket
import uuid
from datetime import datetime, timezone
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import conf
else:
    from scripts.lib import conf


def root() -> Path:
    state = os.environ.get("CONVERSATION_PAUSE_STATE_DIR") or Path.home() / ".local/state/conversation-pause"
    return Path(state) / "script-prompts"


def _path(pane: str, text: str) -> Path:
    words = " ".join(text.split())
    return root() / hashlib.sha256(f"{pane}\0{words}".encode()).hexdigest()


@contextmanager
def locked() -> Generator[None]:
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _matching(pane: str, text: str) -> list[Path]:
    path = _path(pane, text)
    return [*([path] if path.is_file() else []), *root().glob(path.name + ".*")]


def _evict(path: Path) -> None:
    folder = Path(os.environ.get("NOTIFY_STATE_DIR") or Path.home() / ".local/state/notify")
    folder.mkdir(parents=True, exist_ok=True)
    event = {"event_id": uuid.uuid4().hex, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
             "event": "script_evidence_evicted", "cause": "record_limit", "machine": socket.gethostname(),
             "record": path.name, "result": "oldest_removed"}
    with (folder / "auto-silence-events.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.unlink()


def record(pane: str, text: str) -> None:
    if not text.lstrip().startswith("/"):
        return
    limit = conf.read("script_prompts").whole("max_records")
    with locked():
        records = sorted((item for item in root().iterdir() if item.name != ".lock"),
                         key=lambda item: (item.stat().st_mtime_ns, item.name))
        while len(records) >= limit:
            _evict(records.pop(0))
        path = _path(pane, text)
        path.with_name(path.name + "." + uuid.uuid4().hex).touch()


def typed_by_script(pane: str, text: str) -> bool:
    if not pane:
        return False
    return bool(_matching(pane, text))


def typed_here(text: str) -> bool:
    """Consume matching pane and session evidence under the record lock."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    identities = [os.environ.get("HERDR_PANE_ID", ""), "session:" + session if session else ""]
    with locked():
        matched = False
        for identity in set(identities) - {""}:
            paths = _matching(identity, text)
            if paths:
                min(paths, key=lambda path: path.stat().st_mtime_ns).unlink()
                matched = True
        return matched
