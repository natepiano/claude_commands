---
description: Direct authorized merges, gates, builds, and promotion through workers.
argument-hint: "[resume]"
---

# Merge

**Usage:** `/unit:merge [resume]`

## Identity and authority

<MergeIdentity>
Read your unit's staff row and its owning showrunner's staff row.
Read the owning production's document from its staff `doc`.
Keep the production's existing destinations and land-order policy.
The merge unit type grants no push authority.
Apply only the cited contracts from `commands/unit/direct.md`; do not enter its phase loop.

| Contract | Source |
| --- | --- |
| <LongLivedWorkers/>, <DispatchContract/>, <WorkerRuns/> | `commands/unit/direct.md` |
| <WorkerLaunchFailure/> | `docs/direct/launch_failure.md` |
| <WritePromptContract/>, <MergeWorkerCommands/>, <MergeWorkerPaths/> | `docs/direct/write_prompt_contract.md` |
| <AuthorizationContract/> | `docs/direct/authorization.md` |

Apply <MergeCheckpoint/>, <CIPoint/>, and <PromoteMain/> from `commands/showrunner/produce.md` at their named steps.

Direct phase modes grant no merge authority.
Use the unit's staff `worktree` for the director and worker checkout.
Use that checkout's current branch as the staging branch.
Read the publication checkout and branch from the document's merge branch and showrunner checkout lines.
Read publication destinations from that document.
Read current staff paths again after a move or rename.
The worker's `--cwd` does not replace a wrapper's publication checkout.
Promotion follows each production's declared destinations.
</MergeIdentity>

## Startup

<MergeStartup>
1. Set `WORKING_DIR` to the unit's current staff `worktree`.
2. Find the existing run directory through staff and dispatch records.
3. Recover an existing run under <MergeRecovery/> with `SESSION_DIR` set to its path.
4. If no run exists, run `bash ~/.claude/scripts/direct/prepare_session.sh`.
5. For a new run, capture `SESSION_DIR` from its `Session ready at` output before writing order files.
6. If the run lacks `progress_history_state.json`, initialize its recorder:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py start-run \
     --session-dir "$SESSION_DIR" --working-dir "$WORKING_DIR"
   ```

7. Read the production queue, holds, destination tips, and saved operation receipts.
8. Adopt running operations and their receipts without cancellation during rollout.
</MergeStartup>

## Orders

<MergeOrders>
Accept checkpoint notices only within the owning production's authorization rules.
Record the source order, unit, phase, exact checkpoint hash, destination, priority, and required gates before dispatch.
Apply the showrunner's judgments under <MergeCheckpoint/> before execution.
For sdlc, execute only an explicit showrunner land order; a unit notice only supplies queue information.
For hana, follow the showrunner's checkpoint order, holds, gate scope, and publication instructions.
Return conflicts to the owning unit through the showrunner.
Request a new exact hash after the unit resolves a conflict.
</MergeOrders>

## Queue

<MergeQueue>
Keep the queue and operation evidence under the existing run directory.
Permit one mutating merge or promotion operation per destination checkout.
Serialize operations that publish to the same destination branch, including operations from separate staging checkouts.
Hold the next merge during validation and smoke checks under <CIPoint/> and <PromoteMain/>.
Dispatch independent gates and builds concurrently under <MergeWorkerPaths/>.
Preserve the showrunner's order and active holds when a new notice arrives.
</MergeQueue>

## Worker dispatch

<MergeDispatch>
The director runs no long command in its own shell.
Dispatch gates, demo builds, merges, fetches, pushes, CI watches, and promotion through `scripts/direct/assign.sh`.
Apply <LongLivedWorkers/>, <DispatchContract/>, and <WorkerLaunchFailure/>.
Use existing worker roles; command execution can use `--role test`.
When no worker is free, retain the order and wait for a worker result or capacity notice.
The no-worker and launch-failure shell fallbacks do not apply to merge operations.

Store the operation's inputs under `OP="$SESSION_DIR/phases/$ORDER_ID"`.
Apply <WritePromptContract/> with <MergeWorkerCommands/> and <MergeWorkerPaths/>.
Replace the implementation prompt's commit prohibition with the order's exact merge, push, and promotion permissions.
Treat each order as one recorder phase.
Write the order files before opening its phase.
Before the first assign for that order, open its phase:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py start-phase \
  --session-dir "$SESSION_DIR" --phase-id "$ORDER_ID" \
  --phase-title "$ORDER_TITLE" --work-order-file "$OP/prompt.md"
```

Use the retained worker's short name from `worker.py list`:

```sh
bash ~/.claude/scripts/direct/assign.sh --role test --to "$WORKER" \
  --session-dir "$SESSION_DIR" --phase "$ORDER_ID" \
  --paths "$OP/paths.txt" --prompt "$OP/prompt.md" --cwd "$WORKING_DIR"
```

Require every worker to start long commands through `scripts/agents/detached_runs.py start` under <WorkerRuns/>.
The worker completes its assignment only after the result arrives and it reads the result.
For merge execution, exit 3 requires worker-route repair; it permits no attached execution.
Other <WorkerRuns/> cancellation rules apply after rollout.

| Command class | Worker destination |
| --- | --- |
| sdlc `land.sh`, `land_mac_ok.sh`, `queue_runner.sh` | Retained merge worker |
| `merge_checkpoint.py --production sdlc` | Merge worker |
| `merge_gate.py --checkout . --tests-only` | Gate worker |
| `basedpyright scripts/direct scripts/production` | Gate worker |
| Hana gate scripts through `copies.py run --commit <sha>` | Gate worker at the recorded commit |
| `verify.sh check`, `test`, `lint` | Gate worker |
| `cargo fmt`, `cargo test`, `cargo nextest` | Gate worker under the production's existing build rules |
| Cargo release builds | Build worker under the production's existing build rules |
| `smoke_launch.sh` and other production smoke scripts | Gate worker |
| `demo_mac.zsh`, `demo_natedev.zsh` | Separate build workers |
| `gh run watch` and CI collection | CI worker |
| Merge, fetch, push, and promotion commands, including compound shells | Merge worker |
| Long notification loops | Authorized notification worker or the existing group messaging command |
| `ci_points.py` | CI worker |
| `mac_run.sh` | Build worker |
| `validate_and_push.sh` | Merge worker |
| `production_lifecycle.py promote-main` | Merge worker |

These destinations describe jobs, not new worker roles.
Existing wrappers retain their production rules.
</MergeDispatch>

## Gates

<MergeGates>
Dispatch the production's gate commands against the exact staged commit.
Require the reply to name the tested commit, commands, exit codes, and logs.
Do not transfer a green result to a later tree without the production's existing proof rules.
Return failures to the owning unit and showrunner with the failed command and evidence.
For sdlc, the production's merge tests replace CI, smoke, and Mac-run steps.
</MergeGates>

## Bumps

<MergeBumps>
Apply `commands/showrunner/bump.md` under the owning showrunner's order.
Dispatch the urgent checkpoint with `--bump` under <MergeCheckpoint/>.
Keep the existing independent `bump_gate.py` supervisor and its receipt for the pushed tip.
Require the worker to return that receipt, pushed tip, and gate log.
Adopt an existing supervisor after restart; do not start a second gate for its tip.
Keep the urgent order active until its required gate passes.
</MergeBumps>

## Builds and promotion

<MergePromotion>
Dispatch demo builds and smoke checks under <CIPoint/> and <PromoteMain/> and the production's rules.
Require the showrunner's destination authorization before any push or promotion.
Verify the installed artifact's commit before reporting a demo ready.
Preserve each completed push receipt when a later promotion step fails.
</MergePromotion>

## Results

<MergeResults>
1. Read the completed dispatch's saved reply under <LongLivedWorkers/>.
2. Compare its checkpoint, resulting tip, gate results, push receipts, and artifact paths with the order.
3. Verify the destination tip or installed artifact before accepting the result.
4. Record the accepted result in the existing queue or operation receipt before advancing the queue.
5. Close the accepted order's recorder phase:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py finish-phase \
     --session-dir "$SESSION_DIR" --phase-id "$ORDER_ID" --status completed
   ```

Report partial publication precisely; a successful push does not prove promotion completed.
Send the final `send <unit>:` result under <MergeCheckpoint/> through the authorized production route.
</MergeResults>

## Recovery

<MergeRecovery>
Recover the existing run before new dispatches after startup, restart, compaction, account move, or rename.

1. Receive saved messages for the director's agent:

   - Claude: `scripts/message/send.py pending`.
   - Codex: `scripts/message/send.py pending "codex:$CODEX_THREAD_ID"`.
2. Inspect workers with `scripts/production/worker.py list --json`.
3. Read each held run's dispatch replies and `detached_runs` records.
4. Reconcile each held run once with `scripts/agents/detached_runs.py reap --session-dir <run>`.
5. Restore active workers with `scripts/production/worker.py restore`.
6. Read every unaccepted reply before deciding what to repeat.
7. Verify the resulting commit, push receipt, gate record, or artifact under <MergeResults/>.
8. Resume only the authorized work that the evidence proves incomplete.

Keep busy workers and live remote builds running.
Preserve unknown worker states and report the missing evidence.
Keep the same worker and dispatch identity during recovery.
Use the saved launcher receipt before resubmitting any prompt.
An absent local supervisor does not prove that a remote build or push stopped.
</MergeRecovery>

## Reporting

<MergeReporting>
Report to the owning showrunner: unit, phase, checkpoint, result, resulting tip, destination, and evidence paths.
Include worker and dispatch identities, gate receipts, build artifacts, active holds, and the next authorized order when relevant.
Use the production's existing reporting schedule.
Route user reports through the showrunner.
</MergeReporting>
