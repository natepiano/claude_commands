---
description: Take the top-ranked open Hanadocs issue and design it with the user one decision at a time, ending in a design doc.
---

# Implement Issue

Take the highest-priority open issue from the Hanadocs backlog and turn it into a
settled design through a decision-by-decision conversation with the user. The
deliverable is one design doc in the owning repository.

This command does not implement. It creates no worktree, no branch, and no code;
it runs no delegate; it commits nothing; it never edits the issue.

**Usage:**

```text
/implement_issue [<project> | <issue>]
```

- No argument: the open issue with the lowest `backlog_rank`.
- `<project>`: the lowest-ranked open issue for that project, matched against the
  `project` frontmatter property, for example `/implement_issue hana`.
- `<issue>`: an explicit issue path, filename, stem, Obsidian wikilink, or unique
  case-insensitive substring, which overrides rank entirely.

Reject extra arguments with the concise usage message
`Usage: /implement_issue [<project> | <issue>]`.

## Fixed scope

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues/*.md`
- Repositories: checkouts under `/Users/natemccoy/rust`
- Never derive the vault from the current working directory.
- Never inspect or modify issues outside this vault.

`/prioritize` owns the ranking and the background watcher keeps it current. This
command only reads the stored `backlog_rank`. It never writes `backlog_rank`,
`backlog_score`, or any rubric property; never runs `renumber.py` or any other
ranking tool; and never re-litigates the ranking by scoring candidates itself.

## Execution

Execute these stages in order.

### 1. Select the top-ranked issue

Enumerate every issue whose frontmatter says `status: open` and read its stored
`backlog_rank`. Take that value as given; do not verify, recalculate, or refresh
it. Do not choose from filenames remembered from an earlier conversation.

- Resolve a `<project>` argument by normalizing each issue's `project` wikilink
  to its displayed text — treat `'[[hana]]'` and `"[[hana]]"` as `hana` — and
  matching case-insensitively. Select the lowest `backlog_rank` among the
  matches.
- Resolve an `<issue>` argument the way `/close_issue` resolves its selector:
  strip `[[`, `]]`, an optional `|alias`, and an optional `.md`; then try exact
  path, exact filename or stem, title-like stem treating runs of spaces,
  hyphens, and underscores as equivalent, and finally a unique case-insensitive
  substring. Require a regular, non-symlink Markdown file whose real parent is
  the issues directory.
- If the filtered set contains no open issue with a valid `backlog_rank`, stop
  with a concise error naming what was searched — the whole backlog, or the
  project filter that matched nothing, or matched only unranked issues — and
  recommend `/prioritize`. Do not fall back to an unranked issue, infer an order
  from rubric properties, or use file order, modification time, or backlog
  position as a substitute.
- If an `<issue>` selector is ambiguous, show at most five matching open
  candidates and ask which one. Never choose arbitrarily.

### 2. Resolve the owning repository

Resolve the selected issue's `project` to one checkout under
`/Users/natemccoy/rust`. An exact checkout basename is preferred. Many project
labels are not checkout names — `hana_diegetic` is work inside
`/Users/natemccoy/rust/hana`, not its own repository — so inspect the issue's
links, the referenced modules, and Cargo workspace metadata when the basename
does not match.

If the owning repository stays ambiguous, ask one concise question naming the
candidate checkouts. Do not guess.

Read and obey every applicable `AGENTS.md` from the repository hierarchy before
running any repository command.

### 3. Ground the design in live code

Read the complete issue, then verify it against current reality:

- the live implementation and tests around the expected change;
- the project's current goals from its README, roadmap, or design docs;
- existing patterns the design should follow rather than reinvent;
- active worktrees and dirty paths that the work would collide with.

Current code outranks stale issue text. Use `git worktree list --porcelain`,
`git status --short`, targeted `rg`, and focused source/test reads. Prefer LSP
navigation over text search where the language supports it. Do not run full
builds.

When the surface is broad, launch up to three read-only Explore subagents in
parallel over disjoint areas. Give each the issue path, the repository path, the
specific questions to answer, and instructions to report evidence and likely
files without editing anything. Their findings are leads, not decisions.

If live code shows the issue is already implemented, fixed, or obsolete, stop the
design and say so with the specific evidence. Recommend `/close_issue` with the
supported reason.

### 4. Frame the design decisions

Enumerate the decisions the issue actually requires — the product choices,
architectural choices, boundary and naming choices, and scope choices that a
delegated implementer could not make correctly on its own. Order them so
prerequisites come first.

Label them `D1`, `D2`, and so on. Keep each one atomic: a single decision with a
recommendation you can defend from Stage 3 evidence. Do not pad the list with
choices the codebase has already settled.

If the issue needs no design at all — the behavior, surface, and acceptance are
already unambiguous — say so plainly rather than manufacturing decisions. Skip to
Stage 7 and recommend delegation directly.

If `effort` is `⭐⭐⭐⭐⭐`, treat decomposition as `D1`: the first decision is
whether this is one issue or several.

Propose the design doc path before the conversation starts, following the repo
flavor rule from `/plan:to_as_built`:

- **Workspace** (root `Cargo.toml` has `[workspace]`, members under `crates/*`,
  docs organized per-project as `docs/<project>/…`): `docs/<project>/<slug>.md`.
- **Single package** (one crate, flat `docs/`): `docs/<slug>.md`.

Use a lowercase ASCII slug derived from the issue title.

Then present the gate:

```text
## Top priority issue

**<issue title>** — rank <backlog_rank> — <absolute clickable issue path>
Project: <project> | Repository: <absolute repo path>
Goal: <strategic_goal> | Effort: <effort stars> | Stage: <stage>

<one-sentence scope>

<N> design decisions: <D1 label>, <D2 label>, …
Design doc: <absolute proposed design doc path>

start design (recommended — ranked first and validated against live code) / next / stop
```

STOP and wait.

- **start design** proceeds to Stage 5.
- **next** selects the next-ranked open issue in the same filtered set, re-runs
  Stages 2 through 4, and presents the gate again.
- **stop** ends without changes.

### 5. Run the design conversation

Invoke the `adhoc_review` skill via the Skill tool and follow it completely. The
decision list from Stage 4 is its list of items, and the proposed design doc is
its working doc, so it walks the decisions one at a time with necessary context,
one atomic question, a recommendation, still-pending, and an inline choice line.

That skill owns the conversation's rules. In particular, never present a decision
through `AskUserQuestion` or any other survey UI, and never present two decisions
in the same turn.

Carry into every item the evidence gathered in Stage 3: name the exact files,
types, and existing patterns that constrain the decision, and distinguish what
exists from what is proposed. Mark undecided names `(name TBD)`.

If a decision reveals that the issue is materially different from what its text
says — a hidden prerequisite, a product question you cannot answer, or a scope
that should become several issues — surface it as its own decision rather than
absorbing it silently.

### 6. Finalize the design doc

`adhoc_review` records each decision as it is acknowledged. When the walkthrough
completes, shape the accumulated record into a design doc that
`/plan:to_phased_plan` can compile:

- the problem and its connection to the project's current goal;
- each decision with its outcome and the rationale that survived the
  conversation;
- the resulting architecture and the affected surface — specific modules, files,
  and types;
- explicit exclusions that prevent scope growth;
- acceptance criteria as observable behavior plus deterministic verification;
- the repository's build, test, formatting, and lint commands from its
  `AGENTS.md`, quoted exactly — never substitute plain `cargo fmt` where
  `cargo +nightly fmt` is required, and use `cargo nextest run` where the
  repository requires it;
- a link back to the absolute issue path.

Writing this doc is the only filesystem change this command makes. Do not commit
it.

### 7. Hand off and stop

Report the issue, its rank, the repository, and the design doc path. Then
recommend exactly one next command, but do not invoke it:

- **Design settled, needs phases** — `/plan:to_phased_plan` to compile the doc,
  then `/make_a_worktree`, then `/plan:delegate`.
- **Design settled, small and coherent** — `/make_a_worktree`, then
  `/plan:delegate single` with the design doc as context.
- **Decomposed into several issues** — `/issue` for each, then `/prioritize` to
  rank them.
- **Already implemented or obsolete** — `/close_issue` with the supported
  reason.

State that nothing was implemented, no worktree or branch was created, the issue
was not edited, and nothing was committed.

## Invariants

- `/prioritize` chooses the issue; this command never reorders the backlog.
- One issue, one repository, one design doc.
- Read-only until the design doc is written.
- Current code outranks stale issue text.
- The user makes every design decision, one at a time, through `adhoc_review`.
- No worktree, branch, implementation, delegation, issue edit, or commit.
