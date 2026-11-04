---
description: Report build, test, lint, and CI logs.
---

`$ARGUMENTS` is the question.

Step tables, Rebuilds, Memory pressure and Summary count natedev's steps; the Mac section holds the Mac's steps.
Mac copies counts Mac copies of still-compiling natedev tests and estimates the time the winners saved.
The Power profile section compares passing natedev steps before and after each profile change in the last 7 days.
The CPU and disk pressure section shows sampled CPU and IO stall shares, CPU idle and iowait per host.
`~/.claude/scripts/buildlog/buildlog usage-hours [YYYY-MM-DD]` shows hourly averages for build memory, process memory and file cache. It includes build and CI CPU, disk traffic, lowest free disk and free pool tokens.
For questions about memory, CPU or disk by hour, run this command. Answer from its table in a few lines.

Empty, or only a date → run `~/.claude/scripts/buildlog/buildlog report [YYYY-MM-DD]` (default today; its times are in the user's zone) and show its markdown unchanged. Add at most one line after it, only for something the tables do not show.
Then run `python3 ~/.claude/scripts/production/review_regime.py watch`.
Show its line unless it starts with `acknowledged`.
On exit 3, send the line through the registered template:

`python3 ~/.claude/scripts/message/send.py --to user --text-id review_watch.acknowledge --value "session=<this session>" --value "machine=<this machine>" --value "report=<the line>"`

On the Mac, add `--machine natedev`.
Every build report shows the review watch until the user acknowledges it.
On a send failure, report the failure. Use no other phone path.


Otherwise:
1. Run `~/.claude/scripts/buildlog/buildlog schema` once: tables, columns, views, example queries.
2. Write SQL and run `~/.claude/scripts/buildlog/buildlog query "<SQL>"` (read-only; single quotes inside). Add `--json` only when you need exact values to compute from.
3. Answer the question in a few lines, with the numbers. Per kind of step, split by caller unless asked otherwise. Mac rows arrive with natedev's hourly sync.

## On demand only

The report runs only when someone asks for it. No schedule sends it, and no session creates a `report-builds` notifier. User, 2026-10-16: "stop having the builder report be automatic - i want to only have it called on demand".
