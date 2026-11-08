---
description: Manage staff workers through their native conversations.
---

# Unit workers

Run `scripts/lib/py scripts/production/worker.py` from the unit's session.
The caller's native identity selects the parent.
Use the supplied arguments: `$ARGUMENTS`.

1. Use `list [--json]` to show names, dispatch roles, worker states, and activity. Text rows start with row numbers.
2. Use `add <role> [--name <name>] [--json]` to create a worker or reconcile named creation.
3. Use `end <name>` to close a worker and remove its row after confirmed closure.
4. Use `restore` to reopen only workers with active dispatches on their recorded native sessions.
5. Use `check` to list workers without running or resumable evidence.

Keep launchers for assignment and their run's pinned transport.
The default transport stays `mesh`.
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
For a mesh-pinned run, `list` reads the mesh roster through the shared board reader.
For that run, `check` prints incomplete rows and returns 1 only when one exists.
For that run, `restore` prints `unchanged`, returns 0, and changes no pane or staff row.
Other callers use the pane states.
A complete restore rerun prints `unchanged`.
Retire, wrap, and promote retain worker threads. Idle workers stay closed until their next assign.

When the stop budget is unusable, `worker.py end` prints the configuration refusal and exits 7.
The worker row stays.
Correct the named configuration and run `end <name>` again.

Exit codes: `0` complete, `1` check failure, `2` refusal, `3` missing `DONE`, `4` blocked, `5` stalled, `6` busy, `7` kept, `8` uncertain.
