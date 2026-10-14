---
description: Review a caller's diff or the working tree against the style guide.
---

# Direct — style review

**Usage:** `/unit:style_review [since <ref>]`

For a run by hand, apply <StyleReview/> in the current session. The caller
names the reviewed range; a phase passes its baseline as `since <ref>`.
With no range, review the working tree against `HEAD`, including staged,
unstaged, and untracked files. An active `/unit:direct` run is not required.

`/unit:direct` reads this file twice: `<ResolveStyleDiffBase/>` once before the
first dispatch, and `<RunProjectStyleReview/>` once at the end of the run. Both
are defined here in full. Never work from memory of an earlier read — the
`purpose_built=false` question and the after-cleanup reverification are the parts
that go missing.

Everything below is the contract.

---

<StyleReview>
Perform the diff scan and checklist walk inline; do not delegate this review.

1. Read the operation switches with:

   ```bash
   bash ~/.claude/scripts/lint/lint_config.sh export
   ```

   On failure, report the error and stop. If `LINT_OP_STYLE_REVIEW=off`, report
   `Style review: Off` and stop; never report an off check as clean. This
   review runs even when mend and clippy found no issues.
2. Set `STYLE_SINCE` to the caller's baseline or `since <ref>` argument,
   defaulting to `HEAD`. Set `STYLE_DIR` anew for this invocation: use the
   caller's named directory (a run supplies its session directory), or run
   `mkdir -p /tmp/claude` followed by
   `STYLE_DIR=$(mktemp -d /tmp/claude/style-review.XXXXXX)` when none is named.
   Create the caller's directory if needed. On directory creation failure,
   report the error and stop. Keep this directory for every later step.

   Validate the baseline and require the tracked diff to succeed before
   appending untracked files or reading additions:

   ```bash
   if ! git rev-parse --verify --quiet "${STYLE_SINCE}^{commit}" > /dev/null; then
     printf 'Style review: invalid baseline %s\n' "${STYLE_SINCE}" >&2
     exit 1
   fi
   if ! git diff "${STYLE_SINCE}" -- > "${STYLE_DIR}/style-review.diff"; then
     printf 'Style review: tracked diff failed for %s\n' "${STYLE_SINCE}" >&2
     exit 1
   fi
   git ls-files --others --exclude-standard -z \
     | xargs -0 -I{} git diff --no-index /dev/null "{}" 2>/dev/null \
     >> "${STYLE_DIR}/style-review.diff"
   grep '^+' "${STYLE_DIR}/style-review.diff" | grep -v '^+++' > "${STYLE_DIR}/style-review-additions.txt"
   ```

   Either validation or tracked-diff failure reports the ref and stops the
   review; never treat that failure as an empty or passing review.
   `git diff <ref>` includes committed, staged, and unstaged work since that
   baseline. Untracked files are always included as all-additions diffs;
   `git diff --no-index` status 1 means differences found. Keep the headers
   in `.diff` for source locations; `-additions.txt` is the reading aid.
   If the additions file is empty, report `No changes to review.` with the
   range and stop.
3. Unless `=== STYLE_CHECKLIST ===` is already in context, load the guide:

   ```bash
   zsh ~/.claude/scripts/rust_style/load-rust-style.sh
   ```

   Confirm the script's summary line. Walk every numbered rule in the
   checklist against the additions and present a row for each:

   | # | Rule | Result |
   | --- | --- | --- |
   | 1 | Rule name | Pass / **VIOLATION: description at path:line** / Skip (reason) |

   `Pass` means the additions conform; `VIOLATION` names what violates the
   rule and where; `Skip` gives a short reason the rule does not apply.
4. For the banned-words rule, use the canonical scanner; do not enumerate
   stems, write a patterns file, or build a regex:

   ```bash
   python3 ~/.claude/scripts/hooks/banned_words_lib.py --diff < "${STYLE_DIR}/style-review.diff"
   ```

   Exit 0 is `Pass`; exit 1 is `VIOLATION`. Output reports each added line's
   real `path:line`, including untracked files. Pre-existing lines are not
   scanned. Any other exit is an error; report it and stop.
5. Open every violation in the caller's findings ledger when one is supplied,
   for repair through <FixDispatch/>; otherwise report it in the table.
   Report the reviewed range and violation count, or that the review passed.
</StyleReview>

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
4. `purpose_built=false` on the default branch with `other_branches=0`, and no
   branch named in the invocation, needs no user decision either: nothing else
   is in flight whose commits could mix into the style diff. Persist
   `project_base`, say in one line that the run continues on the default branch
   and the style diff starts at `project_base`, and continue. **Never stop to
   ask here** — a run the user left unattended must not idle on a question with
   one reasonable answer.
5. Otherwise `purpose_built=false` — HEAD is detached, or other local branches
   exist — means the run would checkpoint onto a branch it does not own. Ask
   exactly once and dispatch nothing until it is answered:

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
   the range it covers, and read `~/.claude/commands/clippy.md` in full and
   apply it inline with arguments `style-only auto-proceed` — for loop and
   verbose, `style-only auto-proceed since ${STYLE_DIFF_BASE}`. `Off`, error, or
   unresolved choice blocks completion.
5. On successful review, set true and write the result to the marker before any
   cleanup verification. Never clear it during later fixes.
6. Save `${SESSION_DIR}/style_review_after.diff` and
   `${SESSION_DIR}/style_review_after.status`, compare them with the before
   snapshots, and read every style-induced hunk. If Rust/Cargo changed in
   `single`, rerun `verify.sh test` and `lint` for every affected package. Loop
   and verbose skip that per-package pass: <FinalGate/> step 4 reruns
   `verify.sh final` and the workspace `clippy` scan over the cleanup, so a
   per-package pass only runs the same checks twice. Failures use normal
   finding/fix routing.
7. If cleanup reached runnable code, reset smoke to `not_run` and rerun
   <RunApplicationSmokeTest/>. The guard skips this section on return.
8. Continue to <RunPhaseReview/> for `single`, or back to <FinalGate/>.
</RunProjectStyleReview>
