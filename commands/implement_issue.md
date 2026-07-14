---
description: Revalidate an approved simple issue, create its worktree with explicit approval, then implement it through plan-delegate.
---

# Implement Issue

Take one issue approved through `/propose_issue`, create an isolated worktree
interactively, make that worktree the active implementation directory, and run
the existing `plan:delegate` workflow. This command orchestrates; delegated
agents write and review the implementation.

**Usage:**

`/implement_issue issue="<absolute issue path>" repo="<absolute repo path>"`

`$ARGUMENTS` may also include `base="<ref>"`, `worktree="<absolute path>"`, or
`branch="<name>"` overrides. Paths containing spaces must stay quoted.

If arguments are absent, use the single issue and repository explicitly
approved in the current conversation only when both are unambiguous. Otherwise
STOP and ask the user to run `/propose_issue` or provide the explicit handoff.

## Authorization boundary

Approval in `/propose_issue` selects the issue; it does not authorize filesystem
changes. This command must present the exact base commit, worktree path, branch,
dirty-base implications, and immediate delegation behavior, then STOP for a
second explicit approval.

The **approve** response at that gate authorizes:

1. creation of the displayed worktree and branch; and
2. immediate invocation of `plan:delegate` in `single` mode inside that
   worktree after setup succeeds.

It does not authorize pushing, merging, deleting a worktree, closing/editing the
Hanadocs issue, or committing implementation changes. `single` mode leaves the
reviewed implementation uncommitted.

## Execution

Execute these stages in order.

### 1. Parse and validate the handoff

Resolve `issue` to an existing Markdown file under
`/Users/natemccoy/rust/hanadocs/issues`. Resolve `repo` to a Git worktree under
`/Users/natemccoy/rust`. Reject paths outside those roots unless the user
explicitly confirms the exception.

Read:

- the complete issue;
- relevant repository goals and design context;
- the live implementation and tests around the expected change;
- every applicable `AGENTS.md` from the repository hierarchy.

Re-run the essential `/propose_issue` checks. Confirm the issue is still open,
not already implemented, owned by this repository, independent of active work,
small enough for one delegated run, and supported by deterministic acceptance
criteria.

If any check fails, STOP before creating anything. Explain the evidence and
recommend `/propose_issue` again. Do not broaden the issue to rescue the
selection.

### 2. Freeze a bounded work order

Write down, in the conversation state:

- the behavior to add or fix;
- concrete acceptance criteria;
- likely files/modules;
- relevant existing patterns and tests;
- explicit exclusions that prevent scope growth;
- repository-specific build, test, formatting, and lint instructions.

This is a small single-run work order, not a new phased-plan document. If a
credible implementation requires phases, a product choice, or cross-repository
coordination, STOP and send it back to proposal/design rather than continuing.

For Rust projects, follow the applicable `AGENTS.md` exactly. In particular,
use `cargo nextest run` when the repository instructions require it and never
substitute plain `cargo fmt` where `cargo +nightly fmt` is required.

### 3. Resolve the exact base, worktree, and branch

Unless overridden:

- `BASE_REF` is the target checkout's current `HEAD`;
- `BASE_COMMIT` is the immutable commit resolved from `BASE_REF`;
- `WORKTREE_PATH` is a sibling named
  `<repo-basename>_<short_issue_slug>`;
- `BRANCH` is `issue/<short-issue-slug>`.

Use a lowercase ASCII slug, collapse separators, keep the worktree basename
short, and preserve the repository basename. Inspect `git worktree
list --porcelain`, local branches, and filesystem paths. If a collision exists,
suggest a numbered alternative; never reuse or overwrite an existing path or
branch implicitly.

Inspect `git status --short` in the base checkout. A new worktree starts from
the committed `BASE_COMMIT`; uncommitted base-checkout changes are not copied.
List any dirty paths in the approval presentation. Do not stash, commit, reset,
or copy them.

Present:

```
## Ready to create the implementation worktree

- Issue: <absolute issue path>
- Repository: <absolute repo path>
- Base: <branch/ref> at <short BASE_COMMIT>
- Worktree: <absolute WORKTREE_PATH>
- Branch: <BRANCH>
- Existing uncommitted base changes: <none, or paths; these will be excluded>
- Delegation: immediately run plan-delegate in single mode after setup
- Scope: <one sentence>
- Acceptance: <one sentence>

Available actions: **approve**, **modify**, or **stop**.
```

STOP and wait.

- **approve** proceeds with exactly the displayed values.
- **modify** accepts changes, re-runs all collision checks, presents the revised
  values, and waits again.
- **stop** ends without changes.

### 4. Create and verify the approved worktree

After approval, re-check that the path and branch are still absent, then run:

`git -C "<repo>" worktree add "<WORKTREE_PATH>" -b "<BRANCH>" "<BASE_COMMIT>"`

If creation fails, report the command error and STOP. Do not improvise a reset,
branch reuse, or different base.

Verify all of the following before delegation:

- `git -C "<WORKTREE_PATH>" rev-parse HEAD` equals `BASE_COMMIT`;
- `git -C "<WORKTREE_PATH>" branch --show-current` equals `BRANCH`;
- `git -C "<WORKTREE_PATH>" status --short` is empty.

Run:

`bash ~/.claude/scripts/make_a_worktree/copy_settings_local.sh "<WORKTREE_PATH>"`

Report whether a settings file was copied. Do not treat "no source settings
file" as a failure if the helper reports it normally. Re-check
`git -C "<WORKTREE_PATH>" status --short`; if setup introduced an unexpected
dirty path, report it and STOP before delegation.

### 5. Preserve the clean-fix redirect gate

Only for sibling worktrees directly under `/Users/natemccoy/rust`, run the
existing detector:

`python3 ~/.claude/scripts/make_a_worktree/retarget_clean_fix.py detect --repo "<repo-basename>" --worktree "<worktree-basename>"`

If it reports no match, continue silently. If it reports a match, show the
exact redirects and ask **approve** or **skip**. STOP and wait; this is separate
because applying it changes and commits `~/.claude/scripts/clean-fix/clean-fix.conf`.

On approve, run:

`python3 ~/.claude/scripts/make_a_worktree/retarget_clean_fix.py apply --repo "<repo-basename>" --worktree "<worktree-basename>" --commit`

Report the redirects, build addition, and commit result exactly as defined by
the `/make_a_worktree` workflow. On skip, leave the configuration unchanged.
Then continue automatically.

### 6. Make the worktree the implementation directory

Change the command's working directory to `WORKTREE_PATH` and verify `pwd`, Git
top level, branch, and clean status. From this point onward, treat
`WORKTREE_PATH` as the current project directory. Do not run implementation or
verification commands in the base checkout. If the execution environment does
not persist a shell `cd`, pass `WORKTREE_PATH` as the working directory to every
tool and to delegated launchers; the semantic requirement is the same.

### 7. Delegate implementation and review

Invoke the `plan:delegate` skill via the Skill tool and follow it completely.
Pass `single` plus the bounded work order from Stage 2. Include:

- the absolute issue path as source context;
- the exact behavior and acceptance criteria;
- relevant files and existing patterns found during revalidation;
- the explicit exclusions;
- applicable `AGENTS.md` instructions and verification commands;
- a reminder that the Hanadocs issue file is outside implementation scope and
  must not be edited.

Do not ask for another dispatch confirmation: the Stage 3 **approve** response
already authorized this immediate delegation. `plan:delegate` owns
implementation, dual review, fix routing, and its background-wait invariant.
Remain attached until every delegate and review process finishes.

If delegation reveals that the issue is materially larger or architecturally
different from the approved work order, stop at the workflow's decision gate.
Do not silently expand scope.

### 8. Hand back the result

After `plan:delegate` finishes, report:

- issue, worktree path, and branch;
- what behavior was implemented;
- verification and review outcome;
- remaining findings or decisions;
- that changes are uncommitted and nothing was pushed, merged, closed, or
  deleted.

Offer the appropriate next workflow, usually `/commit_prep`, but do not invoke
it without the user's request.

## Failure cleanup

If worktree creation succeeds but a later setup step fails, preserve the
worktree and report its exact path and branch. Never auto-delete it: deletion
has its own guarded workflow and may destroy useful diagnostic state.

## Invariants

- Revalidate before mutating.
- One issue, one repository, one worktree, one bounded delegate run.
- Exact worktree approval is mandatory.
- Delegate agents write implementation code; the primary agent orchestrates.
- No automatic issue edits, commits, pushes, merges, or cleanup.
