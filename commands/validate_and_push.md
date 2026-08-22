---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

Run `~/.claude/scripts/validate_and_push/validate_and_push.sh` with `dangerouslyDisableSandbox: true`.

The script runs local validation, chooses the push path, and pushes directly when branch rules allow it. It does **not** watch CI — on the direct path it stops after the push and prints a `=== CI HANDOFF TO AGENT ===` block with the repo, branch, SHA, and run id. Watching is yours, and you drive it with the tick loop in <WatchCI/>.

Validation requires a clean worktree. Before strict validation, the script automatically applies every fix that cargo-mend marks as machine-applicable; these fixes do not require user approval. Rustfmt and taplo also run in write mode. After each successful fix step, any resulting changes are amended into the last commit (`git commit --amend --no-edit`) and validation continues automatically. Clippy runs only in strict check mode; any findings stop validation for manual fixes.

The cargo-mend fix step follows the `/clippy` workflow's error handling: if an advertised fix fails or is reverted because it does not compile, stop immediately and preserve the reproduction state rather than attempting a manual substitute. After automatic cargo-mend fixes and formatting, strict clippy, configured target checks, tests, and cargo-mend run normally. A fix command failure or any finding under strict validation aborts the workflow.

`validate_ci.sh` ignores `config/lint.conf` for every step except its two cargo-mend steps, so a pre-push gate never silently no-ops. Turning `clippy` off quiets `/clippy`, delegate phases, and the nightly clean-fix pass; it does not quiet this command. Turning `mend` off does quiet this command's mend steps, which print a loud `SKIPPED` line instead — mend rewrites source, so a mend release that emits a fix which does not compile would otherwise block every push from an affected repo with nothing that repo can do about it.

If the script exits with code `2`, the current branch is the default branch and GitHub branch rules require a PR. The script prints JSON with:

- `status: "needs_pr_branch"`
- `commits`
- `proposed_branch`
- `default_branch`

Present those commits and ask:

`Use <proposed_branch> as the PR branch, or provide a different name?`

After the user confirms or provides a branch name, run:

`~/.claude/scripts/validate_and_push/push_pr_branch_and_merge.sh <branch-name>` with `dangerouslyDisableSandbox: true`.

That script creates the PR branch, resets the local default branch to `origin/<default>`, pushes the PR branch, opens the PR, watches checks, merges with rebase when checks pass, deletes the remote branch, switches back to the default branch, and pulls with `--ff-only`. The PR path still blocks on its own watch, because it merges as soon as checks pass; <WatchCI/> applies to the direct-push path only.

If any validation, push, or merge command fails, stop and report the failing step. Do not continue to later steps after a failure.

<WatchCI>
Watch the run on a **3-minute `ScheduleWakeup` tick**. Each tick is one status
query, one status line to the user, and one re-arm. Nothing else.

**Never block on CI and never poll in-band.** No `gh run watch`, no
`run_in_background` watcher, no `sleep`/`until` loop, no repeated queries inside
a single turn. The wakeup *is* the wait. A blocking watch hides per-job progress
for half an hour and defers diagnosis of a red job until the whole run settles.

Every `gh` call takes `dangerouslyDisableSandbox: true` — the sandbox network
proxy breaks its TLS verification.

**Each tick, run exactly this** (substitute repo and run id), then report:

```bash
date '+%H:%M:%S %Z'
gh run view <run-id> --repo <owner/repo> \
  --json createdAt,status,conclusion,jobs \
  --jq '"created: \(.createdAt)  status: \(.status)  conclusion: \(.conclusion)",
        (.jobs[] | select(.status != "completed" or .conclusion != "success")
         | "\(.status) \(.conclusion // "-") \(.name) [\(.databaseId)]")'
```

The `select` prints only jobs that are not yet green, so the tick output shrinks
as the run progresses and a red job is impossible to miss. Drop the `select` on
the first tick to record the full job list.

**Lead every report with the clock time and the run's elapsed time**, computed
from `createdAt`. Format:

`**18:54:05 EDT** · run 32533199159 elapsed **24m 10s** · **11 of 12 green**`

Then one line on what is left, or what broke. That is the whole report — a tick
where nothing changed is two lines, not a recap.

**Re-arm before ending the turn**, always with `delaySeconds: 180`:

- `noop: true` when nothing changed since the last tick, so quiet ticks collapse
  in the user's terminal
- `noop: false` on any tick where a job flipped, you pushed a fix, or the run
  settled
- Put the **full state in the `prompt`** — repo, run id, failing job's
  `databaseId`, what is already confirmed green, and the exact next step. The
  prompt is the only context that survives to the next tick, so it must stand
  alone.

**When a job goes red**, diagnose it that same tick:

1. Fetch the log with `gh api repos/<owner>/<repo>/actions/jobs/<jobId>/logs`.
   Prefer this over `gh run view --log-failed`, which returns nothing in some
   repos.
2. Fix the cause, and verify locally before pushing. Never pipe a cargo command
   whose exit code you need into `tail` — the pipeline reports `tail`'s status,
   so a real failure reads as a pass. Use `set -o pipefail`.
3. Run `cargo mend --fail-on-warn` before the push. It is file-textual, so from
   macOS it still reads `cfg`-gated code that only Linux CI compiles.
4. Commit, push, then **cancel the superseded run** with
   `gh run cancel <old-run-id>` so it stops burning minutes.
5. Re-arm on the new run id with `noop: false`.

Fix and push without stopping to ask. Reach for the user only when the cause is
a genuine tradeoff or a change in scope — a red CI job you know how to fix is
neither.

**When the run concludes green**, call `ScheduleWakeup({stop: true})` and report
the summary block below. If it concludes red and the cause is outside the branch
(infrastructure, a flake you cannot reproduce, a failure already present on the
default branch), say so plainly instead of guessing at a fix.
</WatchCI>

On success, report a compact aligned summary in a fenced `text` block so columns survive rendering:

```text
Validate And Push Complete

Local validation: passed
Tests:            <test summary>
Mend:             <mend summary>
Push:             <push summary>
Commit:           <short commit>
GitHub CI:        <ci summary, including run id and total elapsed>
Final state:      <final branch state>
```

After the block, add one short sentence listing the validation steps that ran.
