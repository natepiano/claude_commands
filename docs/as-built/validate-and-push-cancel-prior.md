# Cancel a superseded CI run (`validate_and_push.sh --cancel-prior`)

## What it is

After a direct push, `validate_and_push.sh --cancel-prior` cancels the branch's older CI runs that are still queued or running, but only for a workflow that already has a run on the pushed sha. A showrunner pushes the merge branch at every green merge checkpoint and at every CI point. Without cancellation, each run waits behind the older ones. CI then spends runner time on shas whose results nobody will read, and the current sha's result arrives late. A rule in `commands/showrunner/produce.md` decides for each push whether to pass the flag. It is passed only when the new push replaces the older run and nothing is waiting on that run's result.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/validate_and_push/validate_and_push.sh` | The `--cancel-prior` flag and `cancel_prior_runs`. |
| `scripts/validate_and_push/push_direct.sh` | Pushes, runs the post-push hook, waits for the sha's run, prints the CI handoff. Unchanged by this feature. |
| `scripts/validate_and_push/test_cancel_prior.sh` | Test with stubbed `git` and `gh`. |
| `commands/showrunner/produce.md` | The cancel-prior rule at <MergeCheckpoint/> (the `merge_checkpoint.py` call) and <CIPoint/> (`ci_points.py ci start`). |

### Data flow

1. Argument parsing sets `CANCEL_PRIOR=true`.
2. `run_validation.sh` runs. If it fails, `set -e` exits before any push, so nothing is cancelled.
3. Both direct paths follow the same order:
   - The `--to <branch>` path exports `PUSH_TARGET_BRANCH` to `push_direct.sh`.
   - The default path runs `push_direct.sh` once `choose_push_path.sh` has chosen `direct`.
   - Each path saves the push's exit code with `bash push_direct.sh || push_status=$?`, calls `cancel_prior_runs` if the flag is set, then runs `exit "$push_status"`.
   - The PR path exits 2 and never cancels, so it ignores the flag.
4. `push_direct.sh` checks up to 40 times, 3 s apart (120 s), for the first run of any workflow on the pushed sha before it returns. `cancel_prior_runs` starts only after that return.

### `cancel_prior_runs`

- **Inputs.** The branch is `PUSH_TARGET_BRANCH`, or `git branch --show-current` when that is unset. The sha is the full `git rev-parse HEAD`, because `gh run list --commit` returns nothing for a short sha.
- **Workflow names come from the pushed sha's own runs:**
  `gh run list --branch B --commit SHA --json workflowName --jq '[.[].workflowName | select(. != null)] | unique[]'`
- **Live runs: one call per workflow and status**, for `queued` and then `in_progress`:
  `gh run list --workflow W --branch B --status S --json databaseId,headSha,status --jq '.[] | "\(.databaseId)\t\(.headSha)\t\(.status)"'`
  Because the status filter runs on the server, completed runs do not count against `gh`'s default limit of 20 runs per call.
- **Cancel:** `gh run cancel ID` for each listed run whose `headSha` differs from the pushed sha.
- `gh`'s stderr always goes to `/dev/null`, and so does the cancel's stdout.

Output lines:

| Line | Stream | Then |
| --- | --- | --- |
| `cancelled run <id> (<sha7>)` (the cancelled run's sha) | stdout | next run |
| `no run for <sha7> yet; nothing cancelled` (the pushed sha) | stdout | return |
| `warning: failed to list runs for <sha7> on <branch>` | stderr | return; nothing cancelled |
| `warning: failed to list <status> runs for <workflow> on <branch>` | stderr | next status |
| `warning: failed to cancel run <id>` | stderr | next run |

### Exit status

- `cancel_prior_runs` returns 0 on every path, so the script exits with `push_direct.sh`'s status.
- When the post-push hook fails, `push_direct.sh` still prints the handoff and exits with the hook's status. The runs are still cancelled and the script still fails.
- After a failed `git push`, the function still runs. An unpushed sha has no run, so it prints the `no run` line and cancels nothing.

### The showrunner rule

The rule appears in `produce.md` at <MergeCheckpoint/> (`merge_checkpoint.py --cancel-prior` passes the flag to the `--quick` push of a green merge, and is refused with `Push: git`) and at <CIPoint/> (`ci_points.py ci start --cancel-prior`). Before the push, check for an older CI run on the merge branch that is still queued or running. Add `--cancel-prior` when this push replaces that run and no CI point is watching its result or diagnosing a red run. The command blocks in those sections leave the flag out; the showrunner adds it push by push.

### The test

Run `bash scripts/validate_and_push/test_cancel_prior.sh`. It exits 0 when all nine checks pass, and it needs `jq` on `PATH`.

- **Setup.** It copies `validate_and_push.sh` and `push_direct.sh` into a `mktemp -d` directory, which is removed on exit. Three stub scripts sit beside the copies:
  - `run_validation.sh` does nothing.
  - `choose_push_path.sh` prints `{"push_path":"direct"}`.
  - `post_push_hook.sh` exits with `$HOOK_EXIT_STATUS`.
- **Command stubs.** Stub `git`, `gh` and `sleep` come first on `PATH`.
  - `git` and `gh` write each call, `%q`-quoted, to `$GIT_CALLS` and `$GH_CALLS`, and fail on any call they do not expect.
  - `sleep` does nothing, so `push_direct.sh`'s 120 s wait returns at once.
- **Fixed runs served by the `gh` stub:**
  - 101: the pushed sha, queued
  - 102: an older sha, queued
  - 103: an older sha, in progress
  - 104: completed
  - 105: queued, in workflow `Other`

  The stub filters by `--workflow` and `--status` with `jq`, then applies the caller's `--jq`.
- **Environment variables that set up failures:** `GH_FAIL_QUERY=successor|queued|in_progress`, `GH_FAIL_CANCEL_ID=<id>`, `GH_NO_SUCCESSOR=1`, `HOOK_EXIT_STATUS=<n>`.
- **What the checks assert:**
  - Every case: exactly one `git push origin HEAD:refs/heads/ci-test-branch`.
  - Normal run: exactly 102 and 103 are cancelled.
  - Query count: the exact query strings, four `run list` calls in all, and no `--limit`.
  - Without the flag: only `push_direct.sh`'s one `run list` and no cancel.
  - A failed cancel moves on to the next run.
  - A failed successor query prints a warning and cancels nothing.
  - A failed `queued` query still cancels 103.
  - A hook failure on each push path keeps its exit status (7 and 9) and still cancels.
  - A missing successor run prints its line and makes no `--workflow` query.

## Invariants

- **Tests never touch real GitHub.** They stub `gh` and `git` and run throwaway copies, so they never push. A new `gh` or `git` call in either script needs a stub branch, because the stubs reject any call they do not expect.
- **Cancellation never changes the push's exit status.** `cancel_prior_runs` runs under `set -euo pipefail` and must return 0 on every path. If an edit lets it fail, its status replaces `push_direct.sh`'s.
- **Without `--cancel-prior`, no cancel query runs.** The only `gh run list` call is `push_direct.sh`'s wait.
- **A workflow is identified by name, taken from the pushed sha's own runs.** Only workflows with a run on the pushed sha are touched. A run on the pushed sha is never cancelled, and neither is a completed run.

## Gotchas

- **Warnings say what failed, not why.** `gh`'s error text is discarded, so a warning names the failed list or run id but not the cause. To see the cause, run the named `gh` call by hand.
- **A late workflow is left alone.** `push_direct.sh` waits up to 120 s for the first run of any workflow on the pushed sha. A workflow whose new run has not appeared by the time `cancel_prior_runs` queries is missing from the name list, so its older runs keep running.

## Why

- **Cancel only when a newer run exists.** Cancelling older runs while the pushed sha has no run would leave the branch with no current CI result.
- **The choice is made for each push, not in workflow config.** Cancelling on every push would also cancel a run a CI point is watching, or a red run under diagnosis. Only the showrunner knows whether such a run exists, so it is a flag the showrunner passes push by push, and no workflow file is edited.
