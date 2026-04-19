Exclude a branch from `/merge_from_branch` discovery in this repo only.

**Arguments**: $ARGUMENTS (branch name to exclude, e.g. `archive/networking-poc`)

If `$ARGUMENTS` is empty, report an error and stop.

Run `bash ~/.claude/scripts/merge_from_branch/exclude_branch.sh "$ARGUMENTS"` with `dangerouslyDisableSandbox: true` — the script writes to `.git/config`, which the sandbox blocks (same pattern as other git write ops).

The branch is appended to `merge-from-branch.exclude` in `.git/config` (local to this clone — never committed). Future `/merge_from_branch` runs will skip it.

Report the script's output to the user.
