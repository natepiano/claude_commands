---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

Run `~/.claude/scripts/validate_and_push/validate_and_push.sh` with `dangerouslyDisableSandbox: true`.

The script runs local validation, chooses the push path, pushes directly when branch rules allow it, and watches CI. It exits successfully only after the direct-push CI path completes successfully.

If the script exits with code `2`, the current branch is the default branch and GitHub branch rules require a PR. The script prints JSON with:

- `status: "needs_pr_branch"`
- `commits`
- `proposed_branch`
- `default_branch`

Present those commits and ask:

`Use <proposed_branch> as the PR branch, or provide a different name?`

After the user confirms or provides a branch name, run:

`~/.claude/scripts/validate_and_push/push_pr_branch_and_merge.sh <branch-name>` with `dangerouslyDisableSandbox: true`.

That script creates the PR branch, resets the local default branch to `origin/<default>`, pushes the PR branch, opens the PR, watches checks, merges with rebase when checks pass, deletes the remote branch, switches back to the default branch, and pulls with `--ff-only`.

If any validation, push, CI, or merge command fails, stop and report the failing step. Do not continue to later steps after a failure.

On success, report a compact aligned summary in a fenced `text` block so columns survive rendering:

```text
Validate And Push Complete

Local validation: passed
Tests:            <test summary>
Mend:             <mend summary>
Push:             <push summary>
Commit:           <short commit>
GitHub CI:        <ci summary>
Final state:      <final branch state>
```

After the block, add one short sentence listing the validation steps that ran.
