# Local operations

State root: `~/rust/hanadocs/morty`.
`current.md` holds items. `checkins.json` holds the current schedule, outstanding inquiries, and the active timer.
Keep only the latest state in both files.

## Records

Read `~/.local/state/staff/staff.json` for showrunner names, parents, and production documents.
Query each showrunner with `~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py questions-table --production <showrunner name>`.
Use the production document from its staff row. Count the returned question rows, including deferred questions.
The old `outstanding/<showrunner name>.json` files now hold reminders. Never use them to count questions.
Save the table, count, source and read time in `open-questions.json`.
Report a failed query as unavailable, never as zero.
Do not restart the old question-message rotation.

For relevant production queues, use the existing next skill and command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/next_item.py show --production <showrunner name> --all-below
```

Resolve root documents from staff. Compare results with the tracking list; they do not replace its priority.
Route decisions to the owning showrunner, which records its answer.

## Messaging

Use the current AGENTS.md messaging rules. Resolve live addresses with:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --json
```

Filter output to the needed owners. Do not load worker rosters for a showrunner inquiry.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to <address> --from morty --text <message>
```

Check the result. Queued is not delivered; delivered is not read. Report failures without inventing a fallback.
Use argument arrays or safe shell quoting for message text.
Send at most one pending inquiry per item. Save the expected reply time before scheduling another check.

## Scheduling

Use `send_later.py` to wake morty, not to send an automatic demand to a showrunner.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/message/send_later.py --at 'YYYY-MM-DD HH:MM' -- --to morty --from morty --text 'Read ~/.claude/commands/associate-producer.md. Read current.md and checkins.json. Handle due items from fresh evidence first. Batch missing status requests by showrunner. Schedule the next wake.'
```

Read back the timer with `systemctl --user show <timer> -p NextElapseUSecRealtime`.
Save its name and actual due time. Do not claim a schedule exists before this succeeds.
Before replacing it, inspect the current recorded timer. Stop only morty's superseded status timers.
If a timer already fired, process its message once. Do not create a second wake for the same checkpoint.
Schedule the minimum of the next two-hour round and any earlier due item.
A round does not move a later owner-promised checkpoint earlier without a reason.
When an earlier checkpoint fires, preserve the existing two-hour round time.
After a round, set its next time two hours later.
If scheduling fails, record the failure and tell nate that automatic checks are unavailable.

## Review links

Use the review_link skill for Markdown and other review files.
Publish through the existing service, then verify HTTP 200 before giving the link.
Refresh an existing link after its source changes.

## Local constraints

Do not edit the live `~/.claude` checkout. Use an isolated worktree for shared code changes.
Do not run builds for status. Required phase and merge checks remain the owners' work.
The review pool is retired. Use current owner roles.
Use `session labels` instead of `unit-marks` in text for nate.
Keep tunable operational values in configuration. Do not start unrelated cleanup.
