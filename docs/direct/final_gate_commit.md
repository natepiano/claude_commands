# Close the run

Read at the point of use from `/unit:direct`. Defines `<FinalGateCommit/>`,
`<RunAsBuilt/>`, `<AsBuiltCommit/>`, `<FinalGate/>`, and `<RunSummary/>` in full.

**Read when:** the run reaches its final gate, or a `single` task finishes.

<FinalGate>
Loop/verbose only after plan exhaustion:

1. Launch one final gate under <BackgroundVerificationContract/>. With a root
   `Cargo.toml`, run `verify.sh final` for the workspace all-targets check and
   full tests. Without a root `Cargo.toml`, run the repository's unit gate.
   For `~/.claude`, use
   `~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --checkout <tree>`.
2. If either gate fails, create a synthetic phase `final` / `Final verification` once:
   capture a new baseline, run `start-phase`, reset review and smoke state, open
   the concrete failures in the ledger, then use its gate plus <FixDispatch/>.
   Later repairs do not repeat those resets. After closure convergence, run
   applicable smoke and return here; do not run phase review or a phase
   checkpoint for the synthetic phase. Rerun the selected gate after each repair.
3. Batch all deferred smoke actions after the gate is green. Ask the user once;
   route discovered defects through the synthetic fix path. If declined, carry
   them as outstanding rather than blocking run completion.
4. Finish the synthetic phase when applicable, run <FinalGateCommit/>, and
   record the final result.

Single mode and early endings skip this gate and state why in <RunSummary/>.
</FinalGate>

<FinalGateCommit>
Loop and verbose only, at most once per run, after <FinalGate/> is green. It
commits synthetic-final repairs and source-comment edits left by the last
phase review, so the run leaves no uncommitted work behind.

1. With no changes in `git status --short`, skip it silently.
2. Confirm the changed paths are only the repairs named by the synthetic
   phase's baseline and any source-comment edit the last phase's plan
   review left in the tree (`~/.claude/docs/direct/phase_end.md` →
   <PhaseEnd/> step 3). Anything else stays uncommitted and is reported
   instead; never sweep an unrelated path into this commit.
3. The repair workers' `lint` covers formatting; do not repeat it here.
4. Stage those paths. In a Cargo repository, run
   `~/.claude/scripts/direct/verify.sh doc` as its own command. On failure,
   repair through <FixDispatch/>, stage the repairs, and run `doc` again.
5. Commit exactly once. Run the commit first in its Bash call, with no command
   before it. Follow the subject and exact-tree rules in <CheckpointCommit/>
   (`commands/unit/checkpoint.md`):

   ```
   checkpoint(<plan-slug>): final gate

   <what the closing repairs changed>

   Change-Review: none — <why nothing visible changes>, or one `<what changed for you> | <how to try it: where and what to type> | <what you see>` line each
   Claude-Session: <session url>
   ```

   Its `Change-Review:` lines follow <CheckpointCommit/>
   (`commands/unit/checkpoint.md`) step 6.

6. This commit holds no phase reservation, so it invokes no drift check and no
   release. Run <PushCheckpoint/> (`commands/unit/checkpoint.md`), then
   report `Final gate <short hash> — closing repairs.` with its
   push note, if any.
</FinalGateCommit>

<RunAsBuilt>
Run once per run, and only on a complete ending: every phase `done` in loop or
verbose, or a finished `single` task. Skip it on a user stop, an open pending
decision, or an error — a partial project has no settled surface to describe —
and state the skip in <RunSummary/>.

Read `~/.claude/commands/plan/to_as_built.md` in full and follow it completely,
with these arguments:

- A phased plan with every phase `done` — pass `${PLAN_DOC}`. Its per-phase
  `As-built` blocks are the change surface, already prepared by
  <RunPhaseShrink/>.
- A `single` ad hoc task with no plan doc — pass `--from-diff`. `single` never
  commits, so the working tree is the change surface.

That command decides every edit, relocation, and deletion itself and never asks
the user, so it needs no gate here. It changes no code and commits nothing itself, so run
<AsBuiltCommit/> after it and leave the run's tree clean. Carry both reports into
<RunSummary/>. A refusal there is reported, not repaired, and leaves nothing to
commit.

When the plan header carries a `> **Issues:**` line, note its issue paths
before `/plan:to_as_built` runs, since it deletes the plan. Once the as-built
doc is written, close each issue:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/proposal_issue.py close <issue> --as-built <doc>
```

`<doc>` is the as-built doc's repo-relative path, e.g.
`docs/hana/as-built/<file>.md`; under `amend`, the doc the work was folded
into. The issues live in the hanadocs vault, outside this worktree, so
<AsBuiltCommit/> never sees them.

Correcting an as-built doc under `docs/as-built/` that contradicts the code is
always allowed in this pass, in any unit's doc, without asking the user, the
showrunner or the owner; a production unit's notice names the file as
`also touches`. User, 2026-10-07.

A production unit sets no run state: the showrunner's tools read it from this run's record, which reads as finished once the run's finish is recorded. A run that stopped, failed, or waits on a decision does not read as finished, and nothing asks for `run done` text in the Plan cell.
</RunAsBuilt>

<AsBuiltCommit>
Loop and verbose only, at most once per run, immediately after <RunAsBuilt/>. It
commits what that step produced — the as-built docs written, updated, or removed
— so the run leaves no uncommitted work behind. `single` never commits: it skips
this and reports the doc edits as uncommitted.

1. With no changes in `git status --short`, skip it silently. An amend that found
   nothing to correct is a valid outcome, not a failure.
2. Confirm the changed paths are documentation only — the docs
   `/plan:to_as_built` reported as created or edited, plus the plan doc and any
   other file it deleted or relocated. A changed
   source file means something other than this step touched the tree: leave
   everything uncommitted, report it, and do not commit.
3. Stage those paths and commit exactly once:

   ```
   docs(<plan-slug>): as-built

   <the docs created, updated, relocated, or removed>

   Claude-Session: <session url>
   ```

   This documentation commit carries no phase and is excluded from
   <PeriodicCI/>'s checkpoint count.
4. This commit holds no phase reservation, so it invokes no drift check and no
   release. Run <PushCheckpoint/>, then report `As-built <short hash> — <n>
   docs.` with its push note, if any.
</AsBuiltCommit>

<RunSummary>
Emit on every multi-phase ending:

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Final gate:** [result or skipped reason]
**As-built:** [docs created or updated, and the commit that carried them; in
`single`, that they are uncommitted; or the reason the run never ran it]
**Style review:** [each phase's reviewed range and result, including Off or a skipped reason]
**Smoke checks still unperformed:** [phase + exact action, or none]
**Deferred decisions still open:** [phase + decision, or none]
**Add-ons awaiting review:** [count, or none]
**CI points:** [each point from `ci_points.log` with its result, the CI branch
merged or kept, or not applicable — fewer than five phases or `single`]
**Reservation disposition:** [checkpointed and outstanding, retained with the
reason this run stopped, or coordination not active]
**Why the run stopped:** [complete, user stop, pending decision, or error]
```

Apply <UserFacingText/> and <RetainPhaseReservation/> for every ending
that did not complete <CheckpointCommit/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Run this command on every exit, without a pipe:

```sh
bash ~/.claude/scripts/direct/end_session.sh
```

Report a non-zero exit and the failed step. Then run
<ReviewPendingAddOns/>; the summary and its commits never wait on it.
</RunSummary>
