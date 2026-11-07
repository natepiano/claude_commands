"""Build commands that send messages to sessions."""
from __future__ import annotations

from pathlib import Path
import sys

from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue


SEND_SCRIPT: Path = Path(__file__).resolve().with_name("send.py")


def send_argv(*arguments: str, send_script: Path | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
    """Build a sender command with the current Python interpreter."""
    return [sys.executable, str(SEND_SCRIPT if isinstance(send_script, UseConfiguredValue) else send_script), *arguments]


def session_send_argv(to: str, *,
    sender: str = "disk_floor",
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[str]:
    """Build the session message command. Read the message text from stdin."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return send_argv("--to", to, "--from", sender,
                     "--timeout", f"{lint_settings.number("sweep_sender_timeout_seconds"):g}")
