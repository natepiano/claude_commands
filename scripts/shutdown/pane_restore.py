"""Resume restored Claude and Codex panes on their retained accounts.

herdr restores the layout without starting agents. A restore opens a tab on the retained account.
It keeps running sessions and occupied panes. It resumes intentionally stopped sessions when requested.

Run: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/pane_restore.py [--session NAME] [--dry-run]
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import sys
from typing import cast

from scripts.herdr import herdr, pane_launch
from scripts.shutdown import pane_record
from scripts.lib import conf
from scripts.shutdown.codex_trust import trust_arguments
from scripts.whoami import agent_accounts
from scripts.whoami.account import named_account_environment
from scripts.shutdown.launch_names import LaunchNames
from scripts.production import staff  # noqa: E402
from scripts.accounts import accounts
from scripts.shutdown import restart

SERVER_TIMEOUT_MS = 60_000


def _resume_saved(session: str, client: herdr.Herdr, pane: herdr.TabSession,
                  original: pane_record.PaneLaunch, entry: pane_record.PaneLaunch, *, reported_unknown: str) -> None:
    if not isinstance(client.pane_activity(pane.pane_id), herdr.PaneAtShell):
        raise herdr.HerdrFailure("the pane runs a program")
    kind = pane_launch.agent_kind(entry)
    home = pane_launch.config_home(entry)
    if isinstance(home, staff.HomeUnknown):
        from scripts.shutdown import member_account

        print(f"anomaly: {entry['session_id']}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind)
        home = staff.SessionHome(selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home)
    account = {"CODEX_HOME": str(home.path)} if kind == "codex" else named_account_environment(home.path)
    title = entry["name"] or pane.label
    launched = herdr.launch_agent(session, pane.workspace, title, entry["cwd"], {**entry["environment"], **account},
        kind, trust_arguments(kind, entry["cwd"], pane_record.resume_arguments(entry)), herdr.NoOpeningPrompt(),
        int(conf.read("shutdown").number("START_SECONDS") * 1000), agent=title, reported_unknown=reported_unknown)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr.HerdrFailure(launched.reason)
    path = pane_record.record_path(session)
    attempt = pane_launch.resume_attempt(path, launched.pane)
    if restart.pane_holds_other_work(client, pane.pane_id, entry["session_id"]):
        return
    pane_launch.finish_restore(path, original, attempt,
        pane_launch.VerifiedResume(entry, frozenset({entry["session_id"]})), close=client.close_tab_confirmed)


def restore(session: str, *, dry_run: bool) -> int:
    client = herdr.Herdr(session)
    names = LaunchNames.read()
    record = names.tab_record
    failures = 0
    try:
        for note in herdr.repair_retained_launches(record, pane_record.record_directory(), dry_run=dry_run):
            print(note)
    except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
        print(f"pane_restore: retained launch repair failed: {error}", file=sys.stderr)
        failures += 1
    selected: dict[staff.StaffId, staff.StaffRow] = {}
    target_accounts: dict[staff.StaffId, accounts.AccountSelection] = {}
    saved_panes: list[herdr.TabSession] = []
    entries = pane_record.read(session)
    live: set[str] = set()
    for pane in client.tab_sessions():
        saved = entries.get(pane.pane_id)
        reference = saved["session_id"] if saved is not None else pane.session_id or ""
        row = record.by_member_reference(reference)
        if row is None:
            if saved is None:
                print(f"{pane.pane_id} {pane.label}: left as it is: no launch entry")
                continue
            saved_panes.append(pane)
            continue
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = parent if parent is not None and restart.pane_workers(parent) else row
        selected[holder.id] = holder
        if saved is not None and holder.id == row.id:
            home = pane_launch.config_home(saved, record=record)
            if isinstance(home, staff.SessionHome):
                target_accounts[holder.id] = (accounts.CodexWorkersAccount(agent_accounts.codex_home_role_label(home.path), home.path)
                    if pane_launch.agent_kind(saved) == "codex" else accounts.NamedAccount(accounts.claude_config_label(home.path), home.path))
            else:
                print(f"anomaly: {row.name}: session home is unknown; select the normal account", file=sys.stderr)
        if dry_run:
            entry = names.resume(saved) if saved is not None else None
            home = pane_launch.config_home(entry, record=record) if entry is not None else staff.HomeUnknown()
            account = str(home.path) if isinstance(home, staff.SessionHome) else "normal account"
            print(f"{pane.pane_id} {row.name}: resume {row.current_session} on {account}")
    for holder in (() if dry_run else selected.values()):
        try:
            with staff.row_operation(str(holder.id)):
                current = staff.initial_snapshot()
                row = current.by_id(holder.id)
                if row is not None:
                    with herdr.restore_staff(current):
                        _ = restart.start_row(row, current, client, target_account=target_accounts.get(holder.id, ""))
        except restart.ClaudeTrustWriteFailed:
            failures += 1
        except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
            failures += 1
            print(f"{holder.name}: not resumed: {error}", file=sys.stderr)
    for pane in saved_panes:
        saved = entries[pane.pane_id]
        try:
            entry = names.resume(saved)
            entry["environment"] = names.environment(entry)
            label = entry["name"] or pane.label
            observed = herdr.read_conversation(entry["session_id"], pane_launch.agent_kind(entry))
            if isinstance(observed, herdr.UnknownConversation):
                herdr.conversation_note(observed)
            if isinstance(observed, herdr.LiveConversation) or entry["session_id"] in live:
                print(f"{pane.pane_id} {label}: left as it is: it already runs")
                continue
            if dry_run:
                home = pane_launch.config_home(entry)
                if isinstance(home, staff.HomeUnknown):
                    print(f"anomaly: {entry['session_id']}: session home is unknown; select the normal account", file=sys.stderr)
                account = str(home.path) if isinstance(home, staff.SessionHome) else "normal account"
                print(f"{pane.pane_id} {label}: resume {entry['session_id']} on {account}")
                continue
            with staff.row_operation(entry["session_id"]):
                current = herdr.read_conversation(entry["session_id"], pane_launch.agent_kind(entry))
                if isinstance(current, herdr.LiveConversation):
                    print(f"{pane.pane_id} {label}: left as it is: it already runs")
                    continue
                if isinstance(current, herdr.UnknownConversation) and not isinstance(observed, herdr.UnknownConversation):
                    herdr.conversation_note(current)
                with herdr.restore_staff(record, labels=names.labels):
                    _resume_saved(session, client, pane, saved, entry,
                        reported_unknown=(current.reason if isinstance(current, herdr.UnknownConversation) else
                            observed.reason if isinstance(observed, herdr.UnknownConversation) else ""))
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
