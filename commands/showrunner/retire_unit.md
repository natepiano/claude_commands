---
description: Retire a production unit.
argument-hint: "<unit> [--dry-run]"
---

# Retire unit

## Run rules

Run in the showrunner session. `<unit>` is the unit's id or stem.

Print the script's output without changes.

## Execution

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/retire_unit.py" <showrunner name> <unit> [--dry-run]
```

Retire finds the unit by its staff name or id under the production.
It stops the unit and its staff workers through `current_session`.
Worker stops keep their rows, native conversations, and stopped panes for restore.
Unknown evidence prints a named anomaly and supplies no stopped proof.
A proved live target or structural fault prevents destructive worktree removal.
Uncommitted work and commits outside the merge branch prevent removal.
The recorder's only added `Project started` line does not count as uncommitted work.

Retire releases build holds, berth reservations, and Mac claims.
It reports other cleanup failures as anomalies.
It removes the worktree, its branch, and any remaining Units row.
The staff row stays with a `RemovedWorktree` location.
Retire parks `.unit.toml` and its retirement facts under the staff id.

Resume uses the staff name or id and restores the folder from the merge branch.
It resumes the row's `current_session` and restores its retained workers.
Only worker end or permanent unit removal deletes worker rows.
An existing run pinned to `mesh` keeps its worker-end path.
A completed retire rerun prints one `unchanged` line, exits 0, and sends no restart notice.
