---
description: Retire one unit of this production. It stops everything the unit owns, marks its Units row retired, and removes its worktree and branch.
argument-hint: "<unit> [--dry-run]"
---

# Retire unit

Run from the showrunner session. `<unit>` is the unit's id or its stem.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/retire_unit.py" <slug> <unit> [--dry-run]
```

When it finishes, print its output exactly as it comes, with nothing added.

It refuses, and changes nothing, when the unit's worktree has uncommitted work or its branch (here
or on origin) has a commit the merge branch does not have. The refusal names each one. The
`Project started` line that the `/unit:direct` recorder adds to a plan, and never commits, is not
uncommitted work when it is the file's only change.

Then it stops everything the unit owns. It finds each thing by its own record, never by a name
pattern, and prints one line for each thing it stops and for each thing it leaves running, with
the reason:

1. Each Claude session that works in the worktree, the unit director and its Claude workers, and the director of each of the worktree's runs wherever it works, stops
   through `stop_resume.stop`. After successful cleanup, its pane launch entry is removed,
   so pane restore does not bring it back.
2. Each run of the worktree with an app-server stops it and ends its Codex workers. `workers.end_unit` then prints one line for each pending message, owed continuation, detached run and reservation it cancels, and for each quarantined entry it removes, and deletes the unit's worker ledger. A worker that cannot end refuses the retire; run it again.
3. The unit's review-pool reviewers retire. A reviewer still reviewing stays.
4. The build holds of the unit's sessions release, its berth reservations release, and the process
   of each Mac claim on the worktree stops.
5. An unowned process inside the worktree blocks retirement; cwd grants no stop authority.

When nothing is left running, the Units row's Plan cell gets
`(retired <date>, worktree removed, session <id>)`, committed on the merge branch (the next merge
push takes it out). The session is the unit director's last one: the live session that carries the
unit's mark, else the newest interactive transcript under the worktree. `/showrunner:resume_unit`
resumes it. Then the worktree, the local
branch and the origin branch are removed. When something still runs, nothing is marked or removed:
the output says what, and a rerun resumes.

`--dry-run` prints each refusal and everything it would stop and remove, and changes nothing.

Exit 0 means the unit is retired. Exit 1 is a refusal, or something still runs.

Never stop a unit's session by hand: no `/exit` into its pane, no direct kill. Use the shared stop API.
