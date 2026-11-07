"""Fixture sender records shared by relay, roster and session lookup tests."""

import json
from pathlib import Path

from scripts.message.relay_identity import publish_relay_identity, relay_session_name


def write_relay_record(state: Path, pid: int, sender: str) -> None:
    publish_relay_identity(pid, sender, state / "relay_senders")


def write_relay_session(registry: Path, state: Path, pid: int, sender: str) -> None:
    """Register a relay whose process lifetime the hook can verify before its socket opens."""
    write_relay_record(state, pid, sender)
    registry.mkdir(parents=True, exist_ok=True)
    _ = (registry / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "name": relay_session_name(sender), "sessionId": f"relay-{pid}",
        "messagingSocketPath": "", "updatedAt": 1,
    }))
