# Delegate — style review

**Usage:** `/plan:delegate_style`

Type this when a run reached its end without the style audit, or when you want
the audit re-run over the project's branch. It runs inside the current session
and already knows the mode, the diff base, and what was committed. If no
delegate run is active, say so in one line and stop.

`/plan:delegate` reads this file twice: `<ResolveStyleDiffBase/>` once before the
first dispatch, and `<RunProjectStyleReview/>` once at the end of the run. Both
are defined here in full. Never work from memory of an earlier read — the
`purpose_built=false` question and the after-cleanup reverification are the parts
that go missing.

Everything below is the contract.

---

<ResolveStyleDiffBase>
Loop and verbose only, once per run, before the first dispatch. `single` skips
it and never sets a base: it commits nothing, so <RunProjectStyleReview/> reads
the working tree directly.

1. If `${SESSION_DIR}/style_diff_base` exists, restore `STYLE_DIFF_BASE` from it
   and return. Later phases never re-resolve the base.
2. Take `<plan-slug>` as the normalized stem derived in <ComposeWorkOrder/>
   step 5 without its `-next.md` suffix — the same slug <CheckpointCommit/>
   writes into every checkpoint subject. Run:

   ```sh
   bash ~/.claude/scripts/delegate/style_branch.sh resolve "${WORKING_DIR}" <plan-slug>
   ```

   Its `project_base` is the parent of this plan's first checkpoint commit, or
   current HEAD when the plan has not checkpointed yet. That base spans a
   project resumed across several runs and still excludes commits the branch
   already carried. Any status other than `ok` records no base: report the
   reason in one line, leave `STYLE_DIFF_BASE` empty, and continue.
3. `purpose_built=true` needs no user decision. Persist `project_base` to
   `${SESSION_DIR}/style_diff_base`, name the branch and how many commits the
   end-of-run style review will therefore cover in one line, and continue.
4. `purpose_built=false` means HEAD is detached or sits on the default branch,
   so the run would checkpoint onto a branch it does not own. Ask exactly once
   and dispatch nothing until it is answered:

   ```
   Each phase checkpoints, and the project-end style review diffs the branch to reach that committed work. Currently <reason>, so this project has no branch of its own to diff. Reply \`branch\` to create \`<suggested_branch>\` here and run on it, \`branch <name>\` to choose the name, or \`stay\` to keep this position and accept that anything else committed here lands in the same style diff.
   ```

   A `branch` answer runs

   ```sh
   bash ~/.claude/scripts/delegate/style_branch.sh create "${WORKING_DIR}" <name>
   ```

   and persists the `project_base` it returns. A non-`ok` status reports its
   reason and re-asks; never create a differently named branch on its own
   initiative, and never move onto an existing one. `stay` persists the
   `project_base` already resolved and says in one line where the style diff
   will start and that unrelated commits landing here join it.
</ResolveStyleDiffBase>

<RunProjectStyleReview>
The run's single style audit, over everything the project built rather than one
phase. Required exactly once when the reviewed diff contains `.rs`,
`Cargo.toml`, or `Cargo.lock`. The actual diff, not `${STYLE_GATE_CONFIG}`,
decides applicability. Phases never run it: they carry no style gate, and a
phase checkpoint never waits on one.

- `single`: after behavioral convergence and first smoke, before phase review.
  The reviewed range is the working tree, tracked and untracked.
- Loop and verbose: from <FinalGate/>, once the whole plan is verified green.
  The reviewed range is `${STYLE_DIFF_BASE}..` — every commit the project
  landed on this branch plus the current working tree.

1. If `STYLE_REVIEW_DONE=true` or the marker exists, restore true and continue.
2. Loop and verbose with an empty `STYLE_DIFF_BASE` have no branch to diff:
   set true, write `not applicable — no diff base` to the marker, and report
   that the run ends without a style review.
3. Build the reviewed diff, including untracked paths. With no Rust/Cargo
   changes in it, set true and write `not applicable` to the marker. Stop if
   the style pass would reach Rust/Cargo work the project did not write.
4. Save combined diff/status to `${SESSION_DIR}/style_review_before.diff` and
   `${SESSION_DIR}/style_review_before.status`, announce the single cleanup and
   the range it covers, and invoke the `clippy` skill inline as
   `style-only auto-proceed` — for loop and verbose, as
   `style-only auto-proceed since ${STYLE_DIFF_BASE}`. `Off`, error, or
   unresolved choice blocks completion.
5. On successful review, set true and write the result to the marker before any
   cleanup verification. Never clear it during later fixes.
6. Save `${SESSION_DIR}/style_review_after.diff` and
   `${SESSION_DIR}/style_review_after.status`, compare them with the before
   snapshots, and read every style-induced hunk. If Rust/Cargo changed, rerun
   `verify.sh test` and `lint` for every affected package. Failures use normal
   finding/fix routing.
7. If cleanup reached runnable code, reset smoke to `not_run` and rerun
   <RunApplicationSmokeTest/>. The guard skips this section on return.
8. Continue to <RunPhaseReview/> for `single`, or back to <FinalGate/>.
</RunProjectStyleReview>
