---
description: Answer a question about build, lint, test and CI timings, failures and flaky tests from the build log, on both machines.
---

`$ARGUMENTS` is the question.

Step tables, Rebuilds, Memory pressure and Summary count natedev's steps; the Mac section holds the Mac's steps.
Mac copies counts Mac copies of still-compiling natedev tests and estimates the time the winners saved.
The Power profile section compares passing natedev steps before and after each profile change in the last 7 days.
The CPU and disk pressure section shows sampled CPU and IO stall shares, CPU idle and iowait per host.

Empty, or only a date → run `~/.claude/scripts/buildlog/buildlog report [YYYY-MM-DD]` (default today; its times are in the user's zone) and show its markdown unchanged. Add at most one line after it, only for something the tables do not show.
Then run `python3 ~/.claude/scripts/production/review_regime.py watch` and show its line under the report, unless it starts `acknowledged`. On exit 3, load `/message_style` if this session has not yet, then also run `python3 ~/.claude/scripts/message/send.py --to user --need decision --action "Acknowledge the review watch in this session." --summary "Review watch waits on your acknowledgment" --text "<the line>"`; on the Mac, add `--machine natedev`. User, 2026-10-04: every build report shows the 12-phase review watch until the user acknowledges it; drop this paragraph once it reads `acknowledged`.

Every phone message says what the user does with `--action`, or says nothing is needed with `--no-action`. Emergency (`--need blocked`) is only for an action the user must take now. A `--need blocked` push also takes `--clears-when "<command that exits 0 when fixed>"`, such as `--clears-when github-warm-status`: send.py cancels the push when that command exits 0. Without it, send.py refuses the push (`REFUSED`, exit 2). On `FAILED` (exit 3), fall back to PushNotification with the `fallback text:` that `send.py` printed.

Otherwise:
1. Run `~/.claude/scripts/buildlog/buildlog schema` once: tables, columns, views, example queries.
2. Write SQL and run `~/.claude/scripts/buildlog/buildlog query "<SQL>"` (read-only; single quotes inside). Add `--json` only when you need exact values to compute from.
3. Answer the question in a few lines, with the numbers. Per kind of step, split by caller unless asked otherwise. Mac rows arrive with natedev's hourly sync.

## On demand only

The report runs only when someone asks for it. No schedule sends it, and no session creates a `report-builds` notifier. User, 2026-10-16: "stop having the builder report be automatic - i want to only have it called on demand".
