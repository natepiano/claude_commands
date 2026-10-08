# Close the run

Read at the point of use from `/unit:delegate`. Defines `<FinalGateCommit/>`,
`<RunAsBuilt/>`, and `<AsBuiltCommit/>` in full.

**Read when:** `<FinalGate/>` is green, or a `single` task finishes.

<FinalGateCommit>
Loop and verbose only, at most once per run, after <FinalGate/> is green. It
commits what the gate itself produced — the style cleanup from step 4 and any
synthetic-final repairs — so the run leaves no uncommitted work behind.

1. With no changes in `git status --short`, skip it silently.
2. Confirm the changed paths are only the ones the gate touched: the
   before/after snapshots in <RunProjectStyleReview/> plus the synthetic phase's
   own baseline name them, plus any source-comment edit the last phase's plan
   review left in the tree (`~/.claude/docs/delegate/phase_end.md` →
   <PhaseEnd/> step 3). Anything else stays uncommitted and is reported
   instead; never sweep an unrelated path into this commit.
3. Run `verify.sh fmt <package>` for every touched package and include the
   result.
4. Stage those paths and commit exactly once:

   ```
   checkpoint(<plan-slug>): final gate

   <what the style pass and any final repairs changed>

   Claude-Session: <session url>
   ```

   Keep the `checkpoint(<plan-slug>)` subject: <ResolveStyleDiffBase/> reads it
   to place a later run's diff base.
5. This commit holds no phase reservation, so it invokes no drift check and no
   release. Run <PushCheckpoint/> (`commands/unit/checkpoint.md`), then
   report `Final gate <short hash> — style review and closing repairs.` with its
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

Correcting an as-built doc under `docs/as-built/` that contradicts the code is
always allowed in this pass, in any unit's doc, without asking the user, the
showrunner or the owner; a production unit's notice names the file as
`also touches`. User, 2026-10-07.

Only after <RunAsBuilt/> and, where it applies, <AsBuiltCommit/> are complete, a production unit runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunners.py" status "<showrunner session>" --unit "<this row's Session cell>" --state run-finished` before <RunSummary/>; a run that stopped, failed, or waits on a decision does not mark itself finished, and nothing asks for `run done` text in the Plan cell.
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

   The subject is deliberately not `checkpoint(<plan-slug>)`. This commit carries
   no phase, and a later run resolving its diff base scans for checkpoint
   subjects — it must not find this one among them.
4. This commit holds no phase reservation, so it invokes no drift check and no
   release. Run <PushCheckpoint/>, then report `As-built <short hash> — <n>
   docs.` with its push note, if any.
</AsBuiltCommit>
