"""Resume restored Claude and Codex panes on their retained accounts.

herdr restores the layout without starting agents. A restore opens a tab on the retained account.
It keeps running sessions and occupied panes. It resumes intentionally stopped sessions when requested.

Run: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/pane_restore.py [--session NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

SCRIPTS = Path(__file__).resolve().parent.parent
for dependency in ("whoami", "message", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import herdr  # noqa: E402
import pane_launch  # noqa: E402
import pane_record  # noqa: E402
from scripts.shutdown.launch_names import LaunchNames
import sessions  # noqa: E402
from scripts.production import staff, staff_sources  # noqa: E402
from scripts.whoami.account import named_account_environment  # noqa: E402

START_TIMEOUT_MS = 90_000
SERVER_TIMEOUT_MS = 60_000


def skip_reason(pane: herdr.TabSession, entry: pane_record.PaneLaunch | None, live: set[str]) -> str:
    """Why the pane stays as it is; empty when it is resumed."""
    if entry is None:
        return "no launch entry" if pane.session_id else ""
    if entry["session_id"] in live:
        return "it already runs"
    return ""


@dataclass(frozen=True)
class ResolvedResume:
    """The saved publication and its current member identity for a restore."""

    original: pane_record.PaneLaunch
    proof: pane_launch.VerifiedResume


def _resume(session: str, client: herdr.Herdr, pane: herdr.TabSession,
            launch: pane_record.PaneLaunch | ResolvedResume) -> None:
    if isinstance(launch, ResolvedResume):
        original, proof = launch.original, launch.proof
    else:
        original = launch
        proof = pane_launch.VerifiedResume(launch, frozenset({launch["session_id"]}))
    entry = proof.expected
    if not isinstance(client.pane_activity(pane.pane_id), herdr.PaneAtShell):
        raise herdr.HerdrFailure("the pane runs a program")
    kind = pane_launch.agent_kind(entry)
    account = ({"CODEX_HOME": entry["config_dir"]} if kind == "codex"
               else named_account_environment(Path(entry["config_dir"])))
    environment = {**entry["environment"], **account}
    title = entry["name"] or pane.label
    launched = herdr.launch_agent(session, pane.workspace, title, entry["cwd"], environment, kind,
        pane_record.resume_arguments(entry), herdr.NoOpeningPrompt(), START_TIMEOUT_MS, agent=title)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr.HerdrFailure(launched.reason)
    path = pane_record.record_path(session)
    attempt = pane_launch.resume_attempt(path, launched.pane)
    # Ordering the tabs keeps each pane's id (herdr review, 2026-10-19).
    fresh = next((item for item in client.tab_sessions() if item.pane_id == pane.pane_id), None)
    if fresh is not None and ((fresh.session_id and fresh.session_id != entry["session_id"])
                              or not isinstance(client.pane_activity(pane.pane_id), herdr.PaneAtShell)):
        print(f"{pane.pane_id}: pane close anomaly: pane holds other work; kept open", file=sys.stderr)
        return
    pane_launch.finish_restore(path, original, attempt, proof, close=client.close_tab_confirmed)


def restore(session: str, *, dry_run: bool) -> int:
    client = herdr.Herdr(session)
    names = LaunchNames.read()
    try:
        if isinstance(names.record, staff.StaffSnapshot):
            for note in herdr.repair_retained_launches(names.record, pane_record.record_directory(), dry_run=dry_run):
                print(note)
    except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
        print(f"pane_restore: retained launch repair failed: {error}", file=sys.stderr)
    entries = pane_record.read(session)
    live = {item["sessionId"] for item in [*sessions.live_sessions(), *sessions.running_sessions()]}
    codex_unknown = ""
    if any(pane_launch.agent_kind(entry) == "codex" for entry in entries.values()):
        try:
            live.update(herdr.running_codex_threads(thread_host=staff_sources.codex_thread_host))
        except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
            codex_unknown = str(error)
            print(f"pane_restore: Codex liveness anomaly: {error}", file=sys.stderr)
    failures = 0
    for pane in client.tab_sessions():
        saved = entries.get(pane.pane_id)
        try:
            entry = names.resume(saved) if saved is not None else None
            if entry is not None:
                entry["environment"] = names.environment(entry)
            if entry is not None and pane_launch.agent_kind(entry) == "codex" and codex_unknown:
                raise herdr.HerdrFailure(f"cannot exclude a running copy: {codex_unknown}")
            label = entry["name"] if entry is not None and entry["name"] else pane.label
            reason = skip_reason(pane, entry, live)
            if entry is None or reason:
                if reason:
                    print(f"{pane.pane_id} {label}: left as it is: {reason}")
                continue
            if dry_run:
                print(f"{pane.pane_id} {label}: resume {entry['session_id']} on {entry['config_dir']}")
                continue
            if saved is None:
                raise ValueError(f"{pane.pane_id}: original launch is unavailable")
            owner = names.tab_record.by_member_reference(saved["session_id"])
            reference = str(owner.id) if owner is not None else saved["session_id"]
            with staff.row_operation(reference):
                current_names = LaunchNames.read()
                entry = current_names.resume({**saved, "session_id": reference})
                entry["environment"] = current_names.environment(entry)
                current_live = {item["sessionId"] for item in [*sessions.live_sessions(), *sessions.running_sessions()]}
                if pane_launch.agent_kind(entry) == "codex":
                    current_live.update(herdr.running_codex_threads(thread_host=staff_sources.codex_thread_host))
                if entry["session_id"] in current_live:
                    print(f"{pane.pane_id} {label}: unchanged")
                    continue
                with herdr.restore_staff(current_names.tab_record, labels=current_names.labels):
                    _resume(session, client, pane, ResolvedResume(saved, current_names.resume_proof(saved, entry)))
            live.add(entry["session_id"])
            print(f"{pane.pane_id} {label}: resumed {entry['session_id']}")
        except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
            failures += 1
            print(f"{pane.pane_id} {pane.label}: not resumed: {error}", file=sys.stderr)
    return 1 if failures else 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("--session", default="")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments)
    session = cast(str, options.session) or herdr.shared_session()
    _ = herdr.start_server(session, SERVER_TIMEOUT_MS)
    return restore(session, dry_run=cast(bool, options.dry_run))


if __name__ == "__main__":
    sys.exit(main())
