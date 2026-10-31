"""Resume each restored herdr pane's Claude on its own account, after a reboot or a server crash.

herdr brings the layout back and starts no agent itself (`resume_agents_on_restore = false`). For
each restored pane whose launch entry (`pane_record`) names a conversation, this opens a tab beside
it with the same title and the entry's account, resumes the conversation there, then closes the
restored tab. herdr review, 2026-10-19: a pane's environment is set when it opens, never typed in.

A pane is left as it is, and named, when its Claude was closed by hand, when a shutdown record owns
its session (`shutdown.py up` brings it back), when the conversation already runs, when its launch flags are unknown, when the account
has no transcript of the conversation, or when the pane runs a program.

Run: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/pane_restore.py [--session NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

SCRIPTS = Path(__file__).resolve().parent.parent
for dependency in ("whoami", "message", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import herdr  # noqa: E402
import pane_record  # noqa: E402
import sessions  # noqa: E402
from scripts.shutdown.stop_resume import resume_arguments  # noqa: E402
from scripts.whoami.account import named_account_environment  # noqa: E402
from shutdown_record import all_session_records  # noqa: E402

START_TIMEOUT_MS = 90_000
SERVER_TIMEOUT_MS = 60_000


def _has_transcript(config_dir: Path, session_id: str) -> bool:
    return any(path.is_file() for path in (config_dir / "projects").glob(f"*/{session_id}.jsonl"))


def skip_reason(pane: herdr.TabSession, entry: pane_record.PaneLaunch | None, owned: set[str], live: set[str]) -> str:
    """Why the pane stays as it is; empty when it is resumed."""
    if entry is None:
        return "no launch entry" if pane.session_id else ""
    if pane.session_id and pane.session_id != entry["session_id"]:
        return f"herdr has {pane.session_id}, the entry {entry['session_id']}"
    if entry.get("ended", False):
        return "its Claude was closed by hand"
    if entry["session_id"] in owned:
        return "a shutdown record owns it"
    if entry["session_id"] in live:
        return "it already runs"
    # Entries from before the field existed were written on Linux, where the flags are read.
    if not entry.get("flags_read", sys.platform != "darwin"):
        return "its launch flags are unknown"
    if not _has_transcript(Path(entry["config_dir"]), entry["session_id"]):
        return f"no transcript of {entry['session_id']} in {entry['config_dir']}"
    return ""


def _resume(session: str, client: herdr.Herdr, pane: herdr.TabSession, entry: pane_record.PaneLaunch) -> None:
    if not isinstance(client.pane_activity(pane.pane_id), herdr.PaneAtShell):
        raise herdr.HerdrFailure("the pane runs a program")
    environment = {**entry["environment"], **named_account_environment(Path(entry["config_dir"]))}
    title = pane.label or entry["name"]
    launched = herdr.launch_agent(session, pane.workspace, title, entry["cwd"], environment, "claude",
        resume_arguments(entry), herdr.NoOpeningPrompt(), START_TIMEOUT_MS, agent=title)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr.HerdrFailure(launched.reason)
    # Ordering the tabs keeps each pane's id (herdr review, 2026-10-19).
    client.close_tab_of(pane.pane_id)


def restore(session: str, *, dry_run: bool) -> int:
    client = herdr.Herdr(session)
    entries = pane_record.read(session)
    owned = {saved["session"]["session_id"] for saved in all_session_records()}
    live = {item["sessionId"] for item in [*sessions.live_sessions(), *sessions.running_sessions()]}
    failures = 0
    for pane in client.tab_sessions():
        entry = entries.get(pane.pane_id)
        reason = skip_reason(pane, entry, owned, live)
        if entry is None or reason:
            if reason:
                print(f"{pane.pane_id} {pane.label}: left as it is: {reason}")
            continue
        if dry_run:
            print(f"{pane.pane_id} {pane.label}: resume {entry['session_id']} on {entry['config_dir']}")
            continue
        try:
            _resume(session, client, pane, entry)
            print(f"{pane.pane_id} {pane.label}: resumed {entry['session_id']}")
        except (herdr.HerdrFailure, OSError) as error:
            failures += 1
            print(f"{pane.pane_id} {pane.label}: not resumed: {error}", file=sys.stderr)
    return 1 if failures else 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("--session", default="")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments)
    session = cast(str, options.session) or herdr.shared_session()
    if not session:
        print("pane_restore: no shared herdr session is set", file=sys.stderr)
        return 2
    # At boot this runs as soon as the server unit starts: wait until it answers.
    _ = herdr.start_server(session, SERVER_TIMEOUT_MS)
    return restore(session, dry_run=cast(bool, options.dry_run))


if __name__ == "__main__":
    sys.exit(main())
