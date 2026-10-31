"""Maintain the watch's notifier job and deliver changes to its showrunner."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from scripts.lib.process_failure import failure_detail
from scripts.production import showrunners
from upgrade_record import Record

SCRIPTS = Path(__file__).resolve().parents[1]


def instance(slug: str) -> str:
    return f"bevy-upgrade-{slug}"


def _notifier(*arguments: str) -> subprocess.CompletedProcess[str]:
    notifier = os.environ.get("BEVY_UPGRADE_NOTIFIER", str(SCRIPTS / "message/notifier.sh"))
    return subprocess.run(["zsh", notifier, *arguments], capture_output=True, text=True, check=False)


def ensure_job(record: Record) -> None:
    words = [str(SCRIPTS / "lib/py"), str(SCRIPTS / "bevy_upgrade/bevy_upgrade.py"),
             "--production", record["production"], "check", "--scheduled"]
    result = _notifier("new", instance(record["slug"]), "--every", "240", "--timeout", "300",
                       "--run", " ".join(shlex.quote(word) for word in words))
    if result.returncode:
        print(f"bevy_upgrade: could not schedule check: {failure_detail(result)}", file=sys.stderr)


def remove_job(slug: str) -> None:
    result = _notifier("remove", instance(slug))
    if result.returncode:
        print(f"bevy_upgrade: could not remove check: {failure_detail(result)}", file=sys.stderr)


def next_check(slug: str) -> datetime | None:
    """None means the notifier has no such job."""
    result = _notifier("status", instance(slug))
    if result.returncode == 1:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("next_due="):
            return datetime.fromtimestamp(int(line.split("=", 1)[1].split()[0]), timezone.utc)
    raise ValueError("notifier status has no next_due line")


def tell_showrunner(slug: str, summary: str, text: str) -> bool:
    address = showrunners.current_address(slug)
    outcome = "no showrunner session"
    if address:
        sender = os.environ.get("BEVY_UPGRADE_SEND", str(SCRIPTS / "message/send.py"))
        try:
            result = subprocess.run([str(SCRIPTS / "lib/py"), sender, "--to", address,
                                     "--from", "bevy-upgrade", "--key", instance(slug),
                                     "--summary", summary, "--text", text],
                                    capture_output=True, text=True, check=False)
        except OSError as error:
            outcome = f"FAILED: {error}"
        else:
            if result.returncode == 0 and result.stdout.strip().startswith("SENT:"):
                return True
            outcome = failure_detail(result)
    print(f"bevy_upgrade: the showrunner was not told: {outcome}", file=sys.stderr)
    return False


def retire_message(slug: str) -> None:
    try:
        address = showrunners.current_address(slug)
        if not address:
            return
        sender = os.environ.get("BEVY_UPGRADE_SEND", str(SCRIPTS / "message/send.py"))
        result = subprocess.run([str(SCRIPTS / "lib/py"), sender, "retire", "--to", address,
                                 "--from", "bevy-upgrade", "--key", instance(slug)],
                                capture_output=True, text=True, check=False)
    except OSError as error:
        outcome = str(error)
    else:
        if result.returncode == 0:
            return
        outcome = failure_detail(result)
    print(f"bevy_upgrade: could not retire the queued message: {outcome}", file=sys.stderr)
