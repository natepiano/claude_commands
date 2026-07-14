---
description: Find one open Hanadocs issue that is current, useful, and small enough for one bounded delegated implementation run.
---

# Propose Issue

Find exactly one great issue to work on next. This command is an investigation
and recommendation workflow only: it never edits an issue, changes source code,
creates a branch or worktree, or starts implementation.

**Usage:** `/propose_issue [optional project, repo, area, or free-text focus]`

The issue vault is always:

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues`
- Project views: `/Users/natemccoy/rust/hanadocs/issues - *.base`

The default scope is every issue file whose frontmatter says `status: open`.
Use `$ARGUMENTS` only to narrow that set. A request such as `hana`,
`repo=/Users/natemccoy/rust/bevy_hana`, or `rendering` narrows the search; it
does not waive any validation below.

## What qualifies

The recommendation must satisfy every condition:

1. **Current:** live code shows the issue is not already implemented, fixed,
   obsolete, or based on an assumption that no longer holds.
2. **Useful:** it has a direct, explainable connection to the project's current
   goals, user experience, or an enabling capability those goals require.
3. **Bounded:** one repository, a small coherent surface, no unresolved product
   or architectural decision, and realistic for one `/plan:delegate` single run.
4. **Independent:** it does not collide with active worktrees, dirty work, an
   in-progress subsystem rewrite, or another prerequisite issue.
5. **Verifiable:** success can be expressed as concrete behavior plus focused
   tests or another deterministic acceptance check.

Reject issues that are merely short to describe. Research spikes, broad
refactors, cross-repository migrations, vague ideas, external coordination,
and changes resting on unstable architecture are not easily accomplishable.

## Execution

Execute these stages in order.

### 1. Establish the product direction

Read the smallest set of current, authoritative local material needed to state
the relevant project's goals: its README, roadmap/design docs, Cargo workspace
metadata, and the issue's linked context. Prefer live repository evidence over
old issue wording. Do not invent an "ultimate goal" from the issue title.

If `$ARGUMENTS` names one or more repositories, use those repositories as the
implementation reality check. Otherwise resolve each plausible issue's
`project` frontmatter to a checkout under `/Users/natemccoy/rust`. An exact
checkout basename is preferred. If a project can map to multiple repositories,
inspect the code and issue links; reject it when the owning repository remains
ambiguous.

Read and obey every applicable `AGENTS.md` before running repository commands.

### 2. Inventory all open issues in scope

Enumerate the files; do not choose from filenames remembered from an earlier
conversation. Read frontmatter and enough body text to understand scope. Apply
the optional focus, then make a plausible shortlist.

Do not trust `priority` as an implementation-readiness score. It is context,
not a substitute for checking current code.

### 3. Divide the shortlist for read-only investigation

When more than four plausible issues remain, launch up to three read-only
Explore subagents in parallel and divide the candidates evenly. Give each
agent:

- the exact issue paths assigned to it;
- the relevant repository paths;
- the five qualification conditions above;
- instructions to inspect live code, tests, worktrees, and repository docs;
- instructions to report evidence, disqualifiers, likely files, and at most two
  candidates.

Subagents must not edit files or run destructive commands. Their findings are
leads, not the final decision. If four or fewer candidates remain, investigate
them directly instead of launching agents for ceremony.

### 4. Validate the finalists yourself

For every finalist, directly verify:

- issue frontmatter still says `status: open`;
- the expected behavior is absent or incorrect in current code;
- no existing test proves it is already supported;
- the repository's active worktrees and dirty paths do not indicate a collision;
- likely implementation files form one coherent surface;
- acceptance can be tested without a manual-only or unavailable environment;
- no prerequisite or unanswered design choice is hiding in the issue.

Use `git worktree list --porcelain`, `git status --short`, targeted `rg`, Cargo
metadata, and focused source/test reads as appropriate. Do not run full builds
for every candidate. Run a focused read-only check only when it materially
changes confidence.

Score finalists privately on goal alignment, user/enabling value, confidence
that the issue is current, boundedness, independence, and verification clarity.
Apply a strong penalty for collision with active work. Select one. Do not show a
leaderboard or pad the answer with runners-up.

### 5. Present one proposal and stop

Use this structure:

```
## Proposed issue

**<issue title>** — <absolute clickable issue path>

**Why this one:** <the value and its connection to a current project goal>

**Live validation:** <specific code/test evidence that it is still open and
not already implemented>

**Bounded implementation:**
- Repository: <absolute repo path>
- Likely surface: <specific modules/files>
- In scope: <concrete behavior>
- Out of scope: <nearby work that must not expand this issue>
- Acceptance: <observable behavior and focused verification>

**Worktree proposal:**
- Base checkout/ref: <repo, branch, and short commit>
- Worktree: <absolute sibling path>
- Branch: <new branch name>

**Confidence:** <high or medium, with the remaining uncertainty if medium>

Available actions: **approve**, **another**, or **stop**.
```

The suggested worktree name is `<repo-basename>_<short_issue_slug>` and the
suggested branch is `issue/<short-issue-slug>`, adjusted only to avoid a live
path or branch collision. The proposal is not authorization to create either.

STOP and wait for the user's decision.

- On **approve**, do not implement or create anything. Reply with the exact
  handoff command, preserving quoted paths with spaces:

  `/implement_issue issue="<absolute issue path>" repo="<absolute repo path>"`

- On **another**, exclude the rejected candidate, repeat the live validation,
  and propose exactly one different issue.
- On **stop**, end without changes.

## Invariants

- One proposal, not a top-three list.
- Read-only from start to finish.
- Current code outranks stale issue text.
- A candidate that needs a design session is not ready for this workflow.
- Never claim work is easy without naming the bounded surface and acceptance
  check that make it so.
