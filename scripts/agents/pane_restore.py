"""Resume restored Claude and Codex panes on their retained accounts.

herdr restores the layout without starting agents. A restore opens a tab on the retained account.
It keeps running sessions and occupied panes. It resumes intentionally stopped sessions when requested.

Run: ~/.claude/scripts/lib/py ~/.claude/scripts/agents/pane_restore.py [--session NAME] [--dry-run]
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
from scripts.herdr import herdr_types
from scripts.agents import pane_record
from scripts.lib import conf
from scripts.agents.codex_trust import trust_arguments
from scripts.whoami import agent_accounts
from scripts.whoami.account import named_account_environment
from scripts.agents.launch_names import LaunchNames
from scripts.production import staff  # noqa: E402
from scripts.accounts import accounts
from scripts.agents import restart

SERVER_TIMEOUT_MS = 60_000


def _resume_saved(session: str, client: herdr.Herdr, pane: herdr.TabSession,
                  original: pane_record.PaneLaunch, entry: pane_launch.CurrentLaunch, *, reported_unknown: str) -> None:
    if not isinstance(client.pane_activity(pane.pane_id), herdr.PaneAtShell):
        raise herdr_types.HerdrFailure("the pane runs a program")
    kind = pane_launch.saved_kind(entry)
    home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
    if isinstance(home, staff.HomeUnknown):
        from scripts.agents import member_account

        print(f"anomaly: {entry['session_id']}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind)
        home = staff.SessionHome(selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home)
    account = {"CODEX_HOME": str(home.path)} if kind == "codex" else named_account_environment(home.path)
    title = entry["name"] or pane.label
    launched = herdr.launch_agent(session, pane.workspace, title, entry["cwd"], {**entry["environment"], **account},
        kind, trust_arguments(kind, entry["cwd"], pane_record.resume_arguments(entry)), herdr.NoOpeningPrompt(),
        int(conf.read("shutdown").number("START_SECONDS") * 1000), agent=title, reported_unknown=reported_unknown)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr_types.HerdrFailure(launched.reason)
    path = pane_record.record_path(session)
    attempt = pane_launch.resume_attempt(path, launched.pane)
    if restart.pane_holds_other_work(client, pane.pane_id, entry["session_id"]):
        return
    pane_launch.finish_restore(path, original, attempt,
        pane_launch.VerifiedResume(entry, frozenset({entry["session_id"]})), close=client.close_tab_confirmed,
        source_pane=pane_launch.ResumeSourcePane(pane.pane_id))


def restore(session: str, *, dry_run: bool) -> int:
    client = herdr.Herdr(session)
    names = LaunchNames.read()
    record = names.tab_record
    failures = 0
    try:
        for note in herdr.repair_retained_launches(record, pane_record.record_directory(), dry_run=dry_run):
            print(note)
    except (herdr_types.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
        print(f"pane_restore: retained launch repair failed: {error}", file=sys.stderr)
        failures += 1
    selected: dict[staff.StaffId, staff.StaffRow] = {}
    target_accounts: dict[staff.StaffId, accounts.AccountSelection] = {}
    saved_panes: list[tuple[herdr.TabSession, pane_record.PaneLaunch]] = []
    entries = pane_record.read(session)
    live: set[str] = set()
    panes = [dict(pane) for pane in client.panes()]
    workspaces = {str(space["workspace_id"]): str(space.get("label") or "") for space in client.listed("workspace")}
    evidence = pane_launch.NativePaneEvidence(panes, client.scan_row_panes(record, panes))
    publications: dict[staff.StaffId, pane_record.PaneLaunch] = {}
    for saved in sorted(entries.values(), key=lambda entry: entry["written_at"]):
        reference = pane_launch.launch_reference(saved, record)
        if isinstance(reference, pane_launch.StaffLaunchReference):
            if record.by_id(reference.id) is not None:
                publications[reference.id] = saved
        else:
            for found in pane_launch.reference_panes(reference, record, client, entries):
                saved_panes.append((herdr.TabSession(str(found["pane_id"]), str(found.get("label") or ""),
                    workspaces.get(str(found.get("workspace_id", "")), ""), reference.session), saved))
    locations = {row.id: pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, client,
                                                   entries, evidence=evidence) for row in record.rows}
    ordered = dict.fromkeys(row.id for pane in panes for row in record.rows if pane in locations[row.id])
    ordered.update(dict.fromkeys(publications))
    for reference in ordered:
        row = record.rows_by_id[reference]
        saved = publications.get(row.id)
        matches = locations[row.id]
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = parent if parent is not None else row
        selected[holder.id] = holder
        if saved is not None and holder.id == row.id:
            home = pane_launch.config_home(saved, record=record)
            if isinstance(home, staff.SessionHome):
                target_accounts[holder.id] = (accounts.CodexWorkersAccount(agent_accounts.codex_home_role_label(home.path), home.path)
                    if pane_launch.agent_kind(saved, record=record) == "codex" else accounts.NamedAccount(accounts.claude_config_label(home.path), home.path))
            else:
                print(f"anomaly: {row.name}: session home is unknown; select the normal account", file=sys.stderr)
        if dry_run:
            try:
                entry = names.resume(saved) if saved is not None else None
            except (ValueError, staff.StaffUnreadable) as error:
                failures += 1
                print(f"{row.name}: not resumed: {error}", file=sys.stderr)
                continue
            home = pane_launch.config_home(entry, record=record) if entry is not None else staff.HomeUnknown()
            account = str(home.path) if isinstance(home, staff.SessionHome) else "normal account"
            identity = str(matches[0]["pane_id"]) if matches else ""
            print(f"{identity} {row.name}: resume {row.current_session} on {account}")
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
        except (herdr_types.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
            failures += 1
            print(f"{holder.name}: not resumed: {error}", file=sys.stderr)
    for pane, saved in saved_panes:
        try:
            entry = names.resume(saved)
            entry["environment"] = names.environment(entry)
            label = entry["name"] or pane.label
            observed = herdr.read_conversation(entry["session_id"], pane_launch.agent_kind(entry, record=record))
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
                current = herdr.read_conversation(entry["session_id"], pane_launch.agent_kind(entry, record=record))
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
        except (herdr_types.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
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
