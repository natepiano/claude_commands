#!/usr/bin/env python3
"""Report local CLI accounts and live weekly quotas without exposing credentials."""

from __future__ import annotations

import asyncio
from datetime import datetime

from agent_accounts import EASTERN, Quota, Report, live_reports
from agent_notes import AGENTS_DIR, read_note


def reset_time(value: datetime | None) -> str:
    if value is None:
        return "unavailable"
    return value.astimezone().strftime("%a %Y-%m-%d %H:%M %Z")


def quota_line(quota: Quota) -> str:
    value = quota.remaining_percent
    remaining = "usage unavailable" if value is None else f"{value:g}% remaining"
    return f"  {quota.label}: {remaining}; resets {reset_time(quota.resets_at)}"


def render(report: Report) -> list[str]:
    lines = [report.tool]
    if report.problem:
        return lines + [f"  {report.problem}"]
    lines.append(f"  Account: {report.email or 'email unavailable'}")
    lines.append(f"  Plan: {report.plan or 'unavailable'}")
    lines += [quota_line(quota) for quota in report.quotas]
    if report.quota_problem:
        lines.append(f"  {report.quota_problem}")
    if report.tool == "Codex":
        count = report.limit_reset_count
        lines.append(f"  Limit resets available: {count if count is not None else 'unavailable'}")
        for expires in report.limit_reset_expirations:
            lines.append(f"  Limit reset expires: {expires.astimezone(EASTERN).strftime('%a %Y-%m-%d %H:%M %Z')}")
        if count and len(report.limit_reset_expirations) < count:
            lines.append("  Some reset expiration dates are unavailable")
    elif report.tool == "Claude" and report.email:
        for path in sorted(AGENTS_DIR.glob("claude *.md")):
            note = read_note(path)
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
    reports = await live_reports()
    print("\n\n".join("\n".join(render(report)) for report in reports))


if __name__ == "__main__":
    asyncio.run(main())
