#!/usr/bin/env python3
"""Report local CLI accounts and live weekly quotas without exposing credentials."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from account import label_for
from agent_accounts import (ClaudeAccountSelection, CodexHomeSource, Quota, Report,
                            codex_home_display, codex_homes, codex_report_in_use, live_reports,
                            seats_home as resolve_seats_home)
import note_file
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402


def reset_time(value: datetime | None) -> str:
    if value is None:
        return "unavailable"
    return value.astimezone().strftime("%a %Y-%m-%d %H:%M %Z")


def quota_line(quota: Quota) -> str:
    value = quota.remaining_percent
    remaining = "usage unavailable" if value is None else f"{value:g}% remaining"
    return f"  {quota.label}: {remaining}; resets {reset_time(quota.resets_at)}"


def render(report: Report, selection: ClaudeAccountSelection, seats_home: Path) -> list[str]:
    multiple_codex_homes = report.tool == "Codex" and len(codex_homes(seats_home)) > 1
    codex_seats = multiple_codex_homes and codex_report_in_use(report, seats_home)
    header = report.tool
    if (multiple_codex_homes and (report.problem or report.email is None)
            and isinstance(report.source, CodexHomeSource)):
        role = ", seats" if codex_seats else ""
        header += f" ({codex_home_display(report.source.home)}{role})"
    lines = [header]
    if report.problem:
        return lines + [f"  {report.problem}"]
    email = report.email
    if email is None:
        lines.append("  Account: email unavailable")
    else:
        label = label_for(report.tool, email)
        roles: tuple[str, ...] = ()
        if report.tool == "Claude" and selection.multiple_accounts:
            roles = selection.roles_for_login(email)
        elif codex_seats:
            roles = ("seats",)
        role = f" ({', '.join(roles)})" if roles else ""
        lines.append(f"  Account: {label}{role} — {email}" if label != email else f"  Account: {email}{role}")
    lines.append(f"  Plan: {report.plan or 'unavailable'}")
    lines += [quota_line(quota) for quota in report.quotas]
    if report.quota_problem:
        lines.append(f"  {report.quota_problem}")
    if report.tool == "Codex":
        count = report.limit_reset_count
        lines.append(f"  Limit resets available: {count if count is not None else 'unavailable'}")
        for expires in report.limit_reset_expirations:
            lines.append(f"  Limit reset expires: {expires.astimezone(user_zone()).strftime('%a %Y-%m-%d %H:%M %Z')}")
        if count and len(report.limit_reset_expirations) < count:
            lines.append("  Some reset expiration dates are unavailable")
    elif report.tool == "Claude" and report.email:
        for path in sorted(note_file.AGENTS_DIR.glob("claude *.md")):
            note = note_file.read_note(path)
            if note and (note.get("login") or "").lower() == report.email.lower():
                expires = note.get("limit_reset")
                count = note.get("limit_reset_count")
                if count and count != "null":
                    lines.append(f"  Limit resets available: {count} (manually recorded)")
                if expires and expires != "null":
                    lines.append(f"  Limit reset expires: {expires} (manually recorded date; time unavailable)")
                break
    return lines


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.accounts import accounts

    selection = accounts.claude_account_selection()
    seats_home = resolve_seats_home()
    reports = await live_reports(selection, seats_home)
    if selection.multiple_accounts:
        reports = [report for report in reports
                   if report.tool != "Claude" or report.email is None
                   or selection.roles_for_login(report.email)]
    print("\n\n".join("\n".join(render(report, selection, seats_home)) for report in reports))


if __name__ == "__main__":
    asyncio.run(main())
