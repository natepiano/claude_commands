"""Maintain the watch's notifier job and deliver changes to its showrunner."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path



from scripts.lib.process_failure import failure_detail
from scripts.production import staff
from scripts.message import session_send
from scripts.bevy_upgrade.upgrade_record import Record

SCRIPTS = Path(__file__).resolve().parents[1]


def instance(staff_id: str) -> str:
    return f"bevy-upgrade-{staff_id}"


def _notifier(*arguments: str) -> subprocess.CompletedProcess[str]:
    notifier = os.environ.get("BEVY_UPGRADE_NOTIFIER", str(SCRIPTS / "message/notifier.sh"))
    return subprocess.run(["zsh", notifier, *arguments], capture_output=True, text=True, check=False)


def command(staff_id: str) -> str:
    """Build the notifier command with its fixed owner reference."""
    return shlex.join([str(SCRIPTS / "lib/py"), str(SCRIPTS / "bevy_upgrade/bevy_upgrade.py"),
                       "--staff-id", staff_id, "check", "--scheduled"])


def ensure_job(record: Record) -> None:
    result = _notifier("new", instance(record["staff_id"]), "--every", "240", "--timeout", "300",
                       "--run", command(record["staff_id"]))
    if result.returncode:
        print(f"bevy_upgrade: could not schedule check: {failure_detail(result)}", file=sys.stderr)


def remove_job(staff_id: str) -> None:
    result = _notifier("remove", instance(staff_id))
    if result.returncode:
        print(f"bevy_upgrade: could not remove check: {failure_detail(result)}", file=sys.stderr)


def next_check(staff_id: str) -> datetime | None:
    """None means the notifier has no such job."""
    result = _notifier("status", instance(staff_id))
    if result.returncode == 1:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("next_due="):
            return datetime.fromtimestamp(int(line.split("=", 1)[1].split()[0]), timezone.utc)
    raise ValueError("notifier status has no next_due line")


def tell_showrunner(staff_id: str, summary: str, text: str) -> bool:
    row = staff.snapshot().by_id(staff.StaffId(staff_id))
    address = f"session:{row.current_session}" if row is not None else ""
    outcome = "no showrunner session"
    if address:
        sender = os.environ.get("BEVY_UPGRADE_SEND", str(session_send.SEND_SCRIPT))
        try:
            result = subprocess.run(session_send.send_argv("--to", address,
                                     "--from", "bevy-upgrade", "--key", instance(staff_id),
                                     "--summary", summary, "--text", text, send_script=Path(sender),
                                     python_interpreter=session_send.PYTHON_LAUNCHER),
                                    capture_output=True, text=True, check=False)
        except OSError as error:
            outcome = f"FAILED: {error}"
        else:
            if result.returncode == 0 and result.stdout.strip().startswith("SENT:"):
                return True
            outcome = failure_detail(result)
    print(f"bevy_upgrade: the showrunner was not told: {outcome}", file=sys.stderr)
    return False


def retire_message(staff_id: str) -> None:
    try:
        row = staff.snapshot().by_id(staff.StaffId(staff_id))
        address = f"session:{row.current_session}" if row is not None else ""
        if not address:
            return
        sender = os.environ.get("BEVY_UPGRADE_SEND", str(session_send.SEND_SCRIPT))
        result = subprocess.run(session_send.send_argv("retire", "--to", address,
                                 "--from", "bevy-upgrade", "--key", instance(staff_id), send_script=Path(sender),
                                 python_interpreter=session_send.PYTHON_LAUNCHER),
                                capture_output=True, text=True, check=False)
    except OSError as error:
        outcome = str(error)
    else:
        if result.returncode == 0:
            return
        outcome = failure_detail(result)
    print(f"bevy_upgrade: could not retire the queued message: {outcome}", file=sys.stderr)
