# Delegate — periodic CI

**Usage:** `/plan:delegate_ci`

Type this when a run skipped a CI point, or to run one now. It runs inside the
current session and already knows the plan doc, the plan slug, and the mode. If
no delegate run is active, say so in one line and stop.

`/plan:delegate` reads this file after every checkpoint and once at the end of
the run. It defines `<PeriodicCI/>` and `<CICleanup/>` in full. Never work from
memory of an earlier read.

Everything below is the contract.

---

<PeriodicCI>
Loop and verbose only; `single` never pushes. Applies only when the plan doc
has five or more phases, counting every `### Phase N — … · status:` heading,
`done` ones included. A shorter plan skips this contract and <CICleanup/>.

Why (user, 2026-09-23): per-phase gates run scoped lints and Linux tests only,
so a long run drifted many phases from a green macOS CI before anyone saw it.

**CI points.** A regular point is due after <CheckpointCommit/> when five or
more of this plan's checkpoints are not yet on the remote CI branch. The final
point runs after <RunAsBuilt/> when at least one is not. Count from git, never
from memory:

```sh
git fetch origin "<ci-branch>" 2>/dev/null
base=$(git rev-parse --verify -q "origin/<ci-branch>" || echo "${STYLE_DIFF_BASE}")
git rev-list --count --grep='^checkpoint(<plan-slug>): ' "${base}..HEAD"
```

With neither a remote branch nor a `STYLE_DIFF_BASE`, count every checkpoint of
this plan reachable from `HEAD`.

**CI branch.** The current branch, pushed under its own name. On the default
branch (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name`), push
to `delegate/<plan-slug>` instead, so unfinished work never lands on the default
branch. Creating that remote branch is authorized by this contract. Append each
remote branch the run creates (one absent from `git ls-remote --heads origin`
before the first push) to `${SESSION_DIR}/ci_remote_branches`.

**A red point blocks the next.** A regular or final point cannot start while an
earlier one is unresolved; repair it first (step 4).

1. **Validate and push.** Between phases only — no dispatch is live and the tree
   is clean after the checkpoint. Run, with `dangerouslyDisableSandbox: true`:

   ```sh
   bash ~/.claude/scripts/validate_and_push/validate_and_push.sh \
     --to "<ci-branch>" \
     --fix-commit "ci(<plan-slug>): validation fixes after phase <N>"
   ```

   `--fix-commit` puts validation fixes in a new commit, so the checkpoint that
   cargo-berth recorded is never amended. Launch it under
   <BackgroundVerificationContract/> and dispatch the next phase only after it
   returns: it needs the clean tree and may commit.
2. **Local failure.** A validation failure is a red point. Open a synthetic phase
   `ci` / `CI point after phase <N>` the way <FinalGate/> step 3 does, repair
   through <FixDispatch/>, commit each repair once as
   `ci(<plan-slug>): <what the repair fixed>`, and rerun step 1. No phase review
   or checkpoint for the synthetic phase.
3. **Watch.** From the script's `=== CI HANDOFF TO AGENT ===` block, record the
   repo, run id, and SHA in `${SESSION_DIR}/ci_watch`, then launch
   `gh run watch <run-id> --repo <repo> --exit-status` with
   `run_in_background: true`. Its task notification is the result; never poll.
   Continue to the next phase meanwhile — the macOS runner is slow, and the
   phase must not wait for it.
4. **Red CI.** When the watch reports failure, let the in-flight phase reach its
   checkpoint, then repair before dispatching another. Diagnose from the failed
   jobs as `commands/validate_and_push.md` `<WatchCI/>` describes, repair through
   the synthetic `ci` phase of step 2, and run a new point at once (step 1),
   without waiting for five more checkpoints. Cancel the superseded run with
   `gh run cancel`. A green result clears `${SESSION_DIR}/ci_watch`.
5. Append `<point> <sha> <run id> <green|red → repaired in <sha>>` to
   `${SESSION_DIR}/ci_points.log` for <RunSummary/>.

The final point must be green before <CICleanup/>; wait on its watch, which is
the `holding` case of <TurnEndGate/>.
</PeriodicCI>

<CICleanup>
Loop and verbose only, after the final <PeriodicCI/> point is green and before
<RunSummary/>. Skip it when `${SESSION_DIR}/ci_remote_branches` is absent or
empty. Ask once:

```
The run pushed <branch list> for CI and the last run is green. Reply `merge` to merge <run branch> into <default branch> with /validate_and_push and delete <remote branch list>, or `keep` to leave everything as it is.
```

This is a `gate` under <TurnEndGate/>; nothing else waits on it.

On `merge`:

1. **Run on the default branch** (the CI branch was `delegate/<plan-slug>`): run
   `/validate_and_push` here with no options; its own PR path applies when branch
   rules require one.
2. **Run on its own branch:** never switch this worktree's branch. Find the
   worktree holding the default branch with `git worktree list`. If it is clean,
   merge there — `git -C <that worktree> merge --ff-only <run branch>`, or
   `--no-ff` when a fast-forward is impossible — then run `/validate_and_push`
   from that worktree. If no worktree holds the default branch, fast-forward it
   with `git fetch . <run branch>:<default branch>`. A dirty default worktree, a
   merge conflict, or a refused fast-forward stops here: report which, and
   delete nothing.
3. After the push succeeds, delete each listed branch with
   `git push origin --delete <branch>`. Local branches stay.

On `keep`: change nothing and name the remote branches in <RunSummary/>.
</CICleanup>
