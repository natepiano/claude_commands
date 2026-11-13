---
description: Manage staff workers through their native conversations.
---

# Unit workers

Run `scripts/lib/py scripts/production/worker.py` from the unit's session.
The caller's native identity selects the parent.
Use the supplied arguments: `$ARGUMENTS`.

1. Use `list [open|closed|all] [--json] [--all-parents]` to inspect workers. The default shows open panes.
2. Use `add <role> [--name <name>] [--json]` to create a worker or reconcile named creation.
3. Use `end <name>` to close a worker and remove its row after confirmed closure.
4. Use `restore` to reopen only workers with active dispatches on their recorded native sessions.
5. Use `check` to list workers without running or resumable evidence.
6. Use `answer <name> --text "<answer>"` to submit an answer in that worker's pane.

The text columns are `#`, `Name`, `Account`, `Activity`, `Pane`, `State`, and `Waiting on`.
Activity shows the active dispatch kind, or the worker's role without an active dispatch.
Waiting on shows live detached run labels, or `-`.
Open panes appear first, with the previous order inside each group.
An account cell includes a foreign machine in parentheses when needed.
Use `list all` to inspect closed or uncertain workers too.
Use `--all-parents` to read saved dispatches for every parent, without a caller staff row.
Use `list all --json --all-parents` for all saved rows without live pane reads.
JSON fields keep their current meaning; the selected filter applies.

Use the assignment launcher with the worker pane.
Pane launchers call `assign <role> <prompt-file>` with these options:

- `--session-dir <run> --slot <slot> --phase <phase> --kind <kind>`
- `--reply <reply-file>`; optional `--name <name>` and `--lens <lens>`
- `--launcher-command '<JSON argument list>'`
- `--fix-cause <cause> --fix-pass <number> --activity <text>` for repairs
- `--early-ready <sentinel>` for a review with two stages

Use `impl`, `test`, `fix`, `review`, `astra-designer`, or `astra-reviewer` as the explicit role.
Selection uses the first idle or resumable worker in role-number order.
The first standard assignment creates the default team.
A rerun uses the same pass and transcript receipt without another submission.
The prompt file remains unchanged.
`list` reads staff membership and pane states.
`check` prints incomplete rows and returns 1 when one exists.
A complete restore rerun prints `unchanged`.
Retire, wrap, and promote retain worker threads. Idle workers stay closed until their next assign.

When the stop budget is unusable, `worker.py end` prints the configuration refusal and exits 7.
The worker row stays.
Correct the named configuration and run `end <name>` again.

For assignment, exit 0 means submitted or accepted for detached compaction.
The pass and any held role stay active.
The launcher returns at once. End the director turn after any assigned synchronous review.
When compaction is due, the launcher prints `<worker>: compaction runs detached; the prompt follows`.
The child survives a signal to the caller's process group.
It requires a compaction record within `WORKER_COMPACT_SECONDS`, then sends the prompt and confirms its transcript receipt.
A detached failure ends the pass with `error` and sends the director a completion notice.
Assignments with no compaction due keep their attached delivery path.
The turn hook saves the result, completes the pass, and sends the director its reply path.
The hook closes the completed pane and keeps its native conversation.
A live run retains the same pass without another prompt.
Other codes: `1` check failure, `2` refusal, `4` Codex question, `6` busy, `7` retained, `8` uncertain.
An attached Codex delivery requires its transcript prompt within `WORKER_SETTLE_SECONDS`; otherwise it returns `8`.
A Claude delivery returns before its transcript must contain the prompt.
A failed delivery command returns an error.
The transcript prevents replay and proves completion.

A worker puts its question in its reply and ends the turn.
An unexpected screen prompt ends no turn, and the director finds it only when it reads the pane.

1. Read the question in the worker reply.
2. Answer with `worker.py answer <name> --text "<answer>"`.
3. End the director turn until the next hook message.

A turn without an assistant reply reports `no DONE` when the transcript records compaction after the accepted dispatch prompt.
Use `answer <name> --text "Read the assigned prompt and do the work."` to resubmit the work.
For an active pass, `answer` adds the saved DONE instruction and keeps the pass and delivery.
The normal turn hook sends the completion notice after the matching DONE reply.
A blocked Codex question receives only the supplied answer text.

A repeated saved launcher reads the receipt and returns without another submission.
