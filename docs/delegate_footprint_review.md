# delegate.md footprint review — cycle 1

Team review of `~/.claude/commands/plan/delegate.md`, run 2026-08-31.
Posture **strengthen**: the workflow's behavior is fixed; the only question is bytes.

Subject at review time: **2385 lines / 18,811 words / 130,256 bytes**, loaded in
full into the orchestrator's context on every `/plan:delegate` invocation.

Five reviewers (`team_review.expert` = gpt-5.6-sol:xhigh, readonly), one lens each:
duplication and canonical home; rationale versus instruction; form and structure;
instruction the scripts already enforce; dead text plus a counterweight pass naming
what must not be cut. 63 findings. Literal before → after text for every finding is
on disk at `/tmp/claude/team_review/2684D714-1628-4739-80E9-0ADA82AA0CE9/cycle1/findings_{1..5}.txt`
and is the source for the edit pass — this file is the consolidated plan, not a
replacement for it.

## Correctness defects (fix regardless of size)

These are not compression. Each is a place where the document is wrong or
contradicts itself, found because five readers read it closely at once.

| # | Where | Defect | Status |
| --- | --- | --- | --- |
| C1 | `<PhaseTeam/>` | "**Only `impl` is given a `${PASS_KIND}`**" contradicted `<LaunchImplementation/>` step 5 ("**All three seats carry a pass kind**") and the recorder, which keys open passes by slot and closes only the calling slot's stale pass (`progress_history.py:1188-1213`, `_pass_slots` / `_close_slot_pass`). The stale rule made the orchestrator launch `test` and `review` with an empty pass kind, which is why those seats showed a role and no duration in the round table. | **fixed** |
| C2 | `<ProgressContract/>` | Describes "Two stage rows" and a reviewer row reading `running (early)`. The recorder emits a round table with seat columns; an armed reviewer occupies the Review seat, not a second stage row. | **fixed** |
| C3 | `<DelegationResultFormat/>` 1638 | "all three tables" — the recorder emits two (`progress_history.py:3298,3310`). The phrase wraps across a line break, so a single-line grep misses it; an earlier pass wrongly cleared this finding on that evidence. | **fixed** |
| C4 | `<BackgroundVerificationContract/>` 134-144 | Says Codex waits with progress disabled, then unconditionally says to arm a timer; `<ProgressContract/>` says Codex never launches that timer. | **fixed** |
| C5 | `<RunSummary/>` 2377 | Lists `convergence reason` as a stop reason. `findings.py` emits only `converged` and `dispatch`; the run cannot stop for it. | **fixed** |
| C6 | `<FindingsLedger/>` | "The main agent owns `abandon`" — `implement.sh:286-315` already records `landed` on clean exit and `abandon --edits-landed` on worker error. The main agent owns it only when the launcher itself disappears. | **fixed** |
| C7 | `<LaunchImplementation/>` 1319-1322 | "no escalation task and no escalated kind" describes the retired difficulty-tier model. Note: the remaining `architect` occurrences are the **live** sibling review and its parsed `skip-architect` switch — preserve those byte-identically until the sibling command is renamed with them. | **fixed** |

## Applied

130,256 → **119,566 bytes** (10,690 cut, 8.2%). 64 sections balance, no dangling
references beyond the three legitimate cross-file ones (`DecisionEconomy`,
`StateAndConsequenceAudit`, `NextItemAmendments`), retired vocabulary gone, and
every entry in the do-not-cut register verified still present.

All seven correctness defects are fixed. Cuts applied, by section:
`<ProgressContract/>`, `<EarlyReviewArm/>`, `<CheckpointCommit/>` steps 5-7,
`<ConsiderNextItems/>`, the reservation trio
(`<DelegatedPhaseReservationContract/>`, `<RetainDelegatedPhaseReservation/>`,
`<CoordinateDelegatedPhaseReservation/>`), `<DelegationResultFormat/>`,
`<FixDispatch/>`, `<TypeTableCells/>`, `<ComposeWorkOrder/>`,
`<VerificationContract/>`, `<PhaseMesh/>`, `<WritePromptContract/>` items 4 and
8, plus the earlier `<FindingsLedger/>`, `<PassOwnership/>`,
`<CoordinationBoard/>`, `<BuildTokenContract/>`, `<PhaseTeam/>`,
`<DiscardPhaseReviewText/>`, and `<LaunchImplementation/>` step 3.

The cut plan below estimated ~30,000 bytes. It landed at 10,690 because roughly
half of what the four cutting lenses proposed was text the counterweight lens
protected, and the register wins: the stale-sentinel story, both timer
absolutes, the 68%-as-36% story at both sites, every JSON record and exact
command, the Not this/This example, and the void-verdict timing rule are all
still here in full. What remains in the file is close to the floor for prose
editing — reaching 40-60% needs D1.

## Applied — read-on-demand split (D1, approved 2026-08-31)

`delegate.md` **119,566 -> 93,092 bytes** (26,474 cut, 22.1%). Against the
130,256 the review started from, **37,164 cut, 28.5%**.

The user approved delegate.md becoming an index, and added the requirement that
drove the naming: an extracted part the user might want to run by hand should be
a slash command, not a passive doc. A slash command file is dual-purpose — the
orchestrator reads it at its moment, and the user can type it when the
orchestrator skips that moment.

**Naming convention.**

| Tier | Location | Name | Command |
| --- | --- | --- | --- |
| Would ever be typed | `~/.claude/commands/plan/` | `delegate_<verb>.md` | `/plan:delegate_<verb>` |
| Never typed | `~/.claude/docs/delegate/` | snake_case of the lead tag | none |

Files under `~/.claude/commands/` register as slash commands, so a subdirectory
there (`commands/plan/delegate/report.md`) would publish `/plan:delegate:report`
and pollute the namespace; the flat `delegate_<verb>.md` form the user proposed
avoids that. One file per *moment*, not per tag: tags that always fire together
share a file named for the lead tag.

**Extracted.**

| File | Tags | Command |
| --- | --- | --- |
| `commands/plan/delegate_report.md` | `<ProgressReport/>` (new; the content half of `<ProgressContract/>`) | `/plan:delegate_report` |
| `commands/plan/delegate_phase_report.md` | `<VerbosePostPhaseReport/>`, `<CombinedWindowReport/>`, `<RemainingWorkOutlook/>` | `/plan:delegate_phase_report` |
| `commands/plan/delegate_checkpoint.md` | `<CheckpointCommit/>` | `/plan:delegate_checkpoint` |
| `commands/plan/delegate_next.md` | `<ConsiderNextItems/>` | `/plan:delegate_next` |
| `commands/plan/delegate_style.md` | `<ResolveStyleDiffBase/>`, `<RunProjectStyleReview/>` | `/plan:delegate_style` |
| `docs/delegate/compose_work_order.md` | `<ComposeWorkOrder/>` | none |

Each stub keeps its tag, so all existing `<Tag/>` call sites resolve unchanged;
the stub names the file, the moment, and a standing prohibition on acting from
memory of an earlier read. `<TagReferenceContract/>` now carries the index table.

**`<ProgressContract/>` was split, not moved.** Timing stays resident because it
fires every tick — the interval, the one-shot timer, both timer absolutes, the
pass/activity distinction, the re-arm. Report *content* moved to
`<ProgressReport/>`. That seam is better than either half of the original plan:
the resident part is small and unmissable, and the moved part is exactly what a
user would want to invoke by hand.

**Two planned extractions were reversed after measuring their call sites.**
`<EarlyReviewArm/>` is referenced from 8 distinct workflow points (state
declaration, dispatch wait, progress tick, two review prompt forms, synthesis,
closure) and the reservation trio from 12, most of them failure branches. Both
stay resident: neither is a one-moment contract, and adding a required file read
inside a failure branch puts the read at the worst possible moment. `~2.5KB`
each left on the table, deliberately.

**Verified after the split:** 65 tags balance across all seven files, no dangling
references beyond the three legitimate cross-file ones, no stale pointers into
moved bodies, and every entry in the do-not-cut register still present.

## Cut plan, ranked by bytes

Every row converged across at least two lenses; the source column names them.
Estimated total **≈30,000 bytes, ~23%**.

| Section | Now | Cut | Consensus | The move |
| --- | ---: | ---: | --- | --- |
| `<ProgressContract/>` | 11,032 | ~6,300 | 5/5 | The recorder owns its output. After "copy the Markdown exactly", ~1.9KB re-describes every table, column, duration, ETA band and clock the script renders. Replace with a byte-for-byte imperative naming the whole output units. |
| `<EarlyReviewArm/>` | 7,760 | ~3,700 | 4/5 | Marker mechanics the recorder enforces (`arm-review` cannot create a pass; the real pass supersedes the marker). **Guarded:** the stale-sentinel story at 1407-1422 and the void-verdict rule are not cuttable — see below. |
| `<CheckpointCommit/>` | 8,504 | ~3,000 | 4/5 | Steps 5-7 become state/result matrices. `claim_state.py` validates ids, cardinality, blockers and alternatives before `state` exists; the orchestrator keeps only the acceptance allowlist and the `protected_tip` comparison. |
| Board + mesh + prompt `## Team` | 8,865 | ~3,200 | 5/5 | Three passages restate one channel split. Canonical homes: board owns the durable record and its commands, `<PhaseMesh/>` owns addresses, `<BuildTokenContract/>` owns mutual exclusion. |
| Token + ledger + pass ownership | 6,524 | ~3,200 | 5/5 | `verify.sh` owns token acquisition and every release path; `findings.py` refuses partial batches and premature verdicts; the recorder rejects unowned pass calls. Keep only what the scripts cannot enforce. |
| `<ConsiderNextItems/>` | 5,765 | ~2,200 | 3/5 | `phase_review.md` already assigns every proposal a `Class: apply\|gate`. The consumer re-derives the classification instead of obeying the field. |
| Reservation contract trio | 6,167 | ~2,000 | 2/5 | Lifecycle facts become one state table. **Guarded:** all four JSON records, every invocation and argument order, and the "current `HEAD` is not proof" passage stay byte-identical. |
| `<DelegationResultFormat/>` + `<FixDispatch/>` | 5,607 | ~1,750 | 2/5 | Result-shape and repair-defense prose becomes absolute dispatch rules. |
| `<TypeTableCells/>` | 1,855 | ~1,150 | 2/5 | Two worked tutorials become one example plus a compact test table. |
| `<ComposeWorkOrder/>` | 4,779 | ~1,000 | 3/5 | Packaging enumeration defers to the already-imported `<DecisionEconomy/>`. The state-and-consequence audit and cross-repository destination rules are unique — keep. |
| `<VerificationContract/>` + prompt item 8 | 2,868 | ~750 | 4/5 | `verify.sh` selects package and integration modes and applies `lint.conf` itself. |
| Phase counting (two sites) | — | ~500 | 3/5 | Compress the chronology. **Guarded:** the 68%-as-36% story and *both* call sites survive — see below. |
| `<PhaseTeam/>` | 2,159 | ~700 | 4/5 | C1 above (~230 done); the rest is a launch procedure restated in full at `<LaunchImplementation/>` and `<FixDispatch/>`. |
| Dead text (C2-C7) | — | ~500 | — | Straight deletions. |

## Do not cut

The counterweight lens named these specifically because the other four lenses
would take them. Each is text that looks like rationale and is the only guard for
a reachable failure.

- **The stale final-diff story** (`<EarlyReviewArm/>` 1407-1422, 1457-1462, 1477-1485). `review.sh` waits for sentinel *existence* and cannot tell whether the sentinel belongs to this phase. Without the full-glob delete, the proof-of-absence, the diff-before-sentinel ordering and the void-verdict rule, an old `final_diff_*.ready` releases the reviewer immediately onto a previous phase's diff and returns fluent false findings. ~2,270 tempting bytes, zero safe.
- **Both timer absolutes** (`<ProgressContract/>` 666-669 and 803-812). They read as one rule twice and prevent opposite defects: running work with no wake-up, and satisfying the Stop hook by re-arming without giving the report already owed. The hook blocks once and cannot compose a report.
- **The phase-count story at both sites.** `progress_history.py` corrects the percentage only when called; it cannot stop a *later* report from counting by hand. `<RemainingWorkOutlook/>` runs after phase completion when no live progress call exists — a cross-reference there is not equivalent.
- **Token self-deadlock and premature-green.** `verify.sh` cannot stop a delegate acquiring the token first, and cannot know whether the peer owning a package has posted `done`.
- **Provider and transport fallbacks.** `codex_mesh=0` is still supported; Claude `--bg` has no reply redirect, so the summary-as-last-act rule is live; a finished Claude session resumes and a finished Codex thread does not.
- **Every literal block**: the four reservation JSON records, the commit-message template, exact authorization strings, `<BroadReviewPrompt/>`, the result template, report tables. Scripts do not reconstruct these; rewording an authorization string changes which reply advances the run.
- **The "Not this / This" progress example** (775-792). The imported guide states the rule; this pair is the only thing that demonstrates turning "edge" and "ancestry" into what the user actually receives.

## Proposed user decisions

**D1 — read-on-demand split. RESOLVED 2026-08-31: approved and applied; see the section above.** The cut plan lands ~23%, not the 40-60% target,
and it is close to the ceiling for prose editing: the reviewers were right to
guard ~8KB of literals, and what remains is mostly load-bearing. Reaching the
target needs a structural change — moving sections the orchestrator needs only at
one moment (the reservation contract, checkpoint recovery, the type-table cells)
out to files it Reads at the point of use, the way `<UserFacingText/>` already
treats `user_facing_explanation.md`. That trades resident footprint for a read
the orchestrator can skip or forget, which is a reliability question, not a
packaging one. Surfaced because being wrong costs a subtly less reliable
orchestrator rather than a one-line revert.

Everything else in this file is recorded, not surfaced: it is either a
correctness fix or a cut with one sensible in-intent outcome. Consensus across
lenses is evidence, not promotion.
