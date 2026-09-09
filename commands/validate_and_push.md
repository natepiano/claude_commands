---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

Run `~/.claude/scripts/validate_and_push/validate_and_push.sh` with `dangerouslyDisableSandbox: true`.

The script runs local validation, chooses the push path, and pushes directly when branch rules allow it. When the default branch lands, on either path, it runs the repo's post-push hook if one exists: `.claude/config/post_push.sh` at the repo root, run with `LANDED_SHA` and `LANDED_BRANCH` set (hana uses it to regenerate and push its public mirror). A hook failure is reported after the push and is the script's exit status; the push itself is never rolled back. It does **not** watch CI — on the direct path it stops after the push and prints a `=== CI HANDOFF TO AGENT ===` block with the repo, branch, SHA, and run id. Watching is yours, and you drive it with the tick loop in <WatchCI/>.

Validation requires a clean worktree. Before strict validation, the script automatically applies every fix that cargo-mend marks as machine-applicable; these fixes do not require user approval. Rustfmt and taplo also run in write mode. After each successful fix step, any resulting changes are amended into the last commit (`git commit --amend --no-edit`) and validation continues automatically. Clippy runs only in strict check mode; any findings stop validation for manual fixes.

The cargo-mend fix step follows the `/clippy` workflow's error handling: if an advertised fix fails or is reverted because it does not compile, stop immediately and preserve the reproduction state rather than attempting a manual substitute. After automatic cargo-mend fixes and formatting, strict clippy, configured target checks, tests, and cargo-mend run normally. A fix command failure or any finding under strict validation aborts the workflow.

`validate_ci.sh` ignores `config/lint.conf` for every step except its two cargo-mend steps, so a pre-push gate never silently no-ops. Turning `clippy` off quiets `/clippy`, delegate phases, and the scheduled style-fix pass; it does not quiet this command. Turning `mend` off does quiet this command's mend steps, which print a loud `SKIPPED` line instead — mend rewrites source, so a mend release that emits a fix which does not compile would otherwise block every push from an affected repo with nothing that repo can do about it.

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
query, one stage-status table to the user, and one re-arm. Nothing else.

**Never block on CI and never poll in-band.** No `gh run watch`, no
`run_in_background` watcher, no `sleep`/`until` loop, no repeated queries inside
a single turn. The wakeup *is* the wait. A blocking watch hides per-job progress
for half an hour and defers diagnosis of a red job until the whole run settles.

Every `gh` call takes `dangerouslyDisableSandbox: true` — the sandbox network
proxy breaks its TLS verification.

**Each tick, run exactly this** (substitute repo and run id):

```bash
date '+%H:%M:%S %Z'
gh run view <run-id> --repo <owner/repo> \
  --json createdAt,status,conclusion,jobs \
  --jq 'def secs: if . >= 60 then "\((./60)|floor)m \((.%60)|floor)s" else "\(.|floor)s" end;
        def icon: if .status != "completed" then "…"
                  elif .conclusion == "success" then "green"
                  elif .conclusion == "skipped" then "skipped"
                  elif .conclusion == "cancelled" then "cancelled"
                  else "RED" end;
        ((.createdAt|fromdateiso8601) as $c | (now - $c)) as $elapsed
        | ([.jobs[]|select(.conclusion=="success")]|length) as $g
        | ([.jobs[]|select(.conclusion=="skipped")]|length) as $s
        | "run \(.status) \(.conclusion // "-") · elapsed \($elapsed|secs) · \($g) of \(.jobs|length) green\(if $s > 0 then " (\($s) skipped)" else "" end)",
          "| stage | status | time |",
          "|---|---|---|",
          (.jobs[]
           | (if .startedAt == null or (.startedAt|startswith("0001")) then null
              else (.startedAt|fromdateiso8601) end) as $b
           | (if .completedAt == null or (.completedAt|startswith("0001")) then now
              else (.completedAt|fromdateiso8601) end) as $e
           | "| \(.name) | \(icon) | \(if $b == null then "-" else (($e-$b)|secs) end) |")'
```

Never write `\"` inside the `--jq '...'` expression. The shell's single quotes
already protect the double quotes, and the backslashes make jq fail to parse
with `unexpected token "\\"`.

The command emits the finished table, ready to paste. It prints **every stage on
every tick**, not only the ones still moving, so each report is a standing
picture of the run instead of a diff the user has to reassemble from earlier
ticks. A stage that has not started yet shows `-` for time; a running one shows
time elapsed so far, recomputed at each tick.

`skipped` is a normal conclusion for a conditional stage, not a failure. It is
excluded from the green count, which is why a fully successful run can read
`9 of 11 green (2 skipped)`. Never report a skipped stage as broken or as
blocking the run — the run-level `conclusion` is what settles it.

**Lead every report with the clock time and the run's elapsed time**, then the
table:

`**18:54:05 EDT** · run 32533199159 elapsed **24m 10s** · **9 of 11 green (2 skipped)**`

| stage | status | time |
|---|---|---|
| Format Check | green | 17s |
| Test Suite | … | 4m 55s |
| cargo-mend Build Check | skipped | 0s |

Then at most one line on what is left, or what broke. The table carries the
detail — do not narrate it back row by row, and do not recap earlier ticks.

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
