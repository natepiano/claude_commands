# Review pool: one permanent reviewer per unit

> **Production:** build-followups (enh-showrunner-unit)

## Why

User, 2026-10-15: reviewers should never restart. Each unit director keeps its own reviewer, which goes on reviewing whatever the unit works on and builds up context about it. The pool grows only when a unit needs two reviews at once.

What happens today: hana's tool-based-ui pool has split `hash`, 22 reviewer records, names up to `code81`, and 20 idle reviewers still holding leases for long-finished phases. The cause is in `review_pool.py` `_choose`. A lease is keyed by `(unit, phase)`, so a new phase never matches the unit's own reviewer. When no unleased reviewer is free, a new one is forked from the template, and with two lenses that is two new reviewers each phase. Nothing is ever reused.

## Design

1. **One reviewer per unit.** A unit's first review forks its reviewer from the type's template. Every later review resumes that same Codex thread. A lease is keyed by the unit alone; the phase and pass are recorded on the turn, not on the lease.
2. **Idle means a saved thread.** Between reviews no process runs. "Restart on need" is `follow` resuming the thread id.
3. **Concurrency by fork.** When every reviewer of the unit is busy, `allocate` forks the unit's most recently used reviewer (not the template), so the fork starts with the unit's context. The fork stays as that unit's second reviewer. A unit grows only to the number of reviews it runs at once.
4. **Never reset.** A reviewer ends only when its unit's run ends or the unit is retired. A full thread relies on the launcher's existing `compact` above `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS`.
5. **No arm split.** `arm`, `split`, the `hash`/`own` comparison and `REVIEW_LEASE` are removed, and every review goes to the unit's reviewer. The phase-end `end-lease` reset and `health`'s stale-lease ending are removed.
6. **A changed standing prompt reaches live reviewers without a reset.** `codex_mesh._resume_params` sends the roster entry's `instructions_file` on every resume. So `prime`, after a memory change, points each live reviewer's roster `instructions_file` at the new copy, and its next turn carries the new standing prompt. Today the reviewer is retired instead.
7. **UI reviewer too.** The `ui` type follows the same rule, and the `--fresh` reset per design check goes. Unit memory is prepended on a reviewer's first turn only, for both types.
8. **`/review:direct` keeps two jobs: memory curation and priming.** Curation means reading merged phases' findings and writing `memory/<unit>.md` and `memory/repository.md`. Priming means building `standing/<type>.md` and running `prime`. Its hourly health timer and the dailies trial report go, since there is no trial and no lease left to age. The case for keeping it is that memory is the one thing a permanent reviewer cannot gather from its own thread: patterns across units and across repositories.

## Phases

### Phase 1 — per-unit allocation and migration (`review_pool.py`)
- `PhaseLease` becomes `UnitLease(unit)`; `served` is dropped. The `allocate` order is:
  1. an idle reviewer of the type already leased to the unit;
  2. otherwise, a fork of that unit's most recently allocated reviewer of the type;
  3. otherwise, for the unit's first review, a fork of the template.
  `--phase` and `--pass` stay on `allocate` for the turn record only.
  When the fork of the unit's reviewer fails, the new reviewer is forked from the template instead.
- The phase-end `end-lease` step (`docs/delegate/phase_end.md`) goes, and `end-lease --phase` ends nothing, so a director still running the old step leaves its reviewer in place.
- `migrate [--apply]`: retire the reviewers whose unit director is not running, read from `unit_lookup.py`'s session marks (a unit whose state is unknown counts as running). For each running unit, keep its most recently allocated reviewer per type as its permanent reviewer, and retire the rest, unleased ones included. A lease that names `trunk` matches the unit `trunk-unit`. It prints the plan without `--apply`; the showrunner runs `--apply`.
- Tests: two phases of one unit reuse one reviewer; two concurrent reviews fork from the unit's reviewer, not the template; migrate on a copy of the tool-based-ui layout.

### Phase 2 — remove the reset and the split
- Remove `arm`, `split`, `end-lease` and `health`'s lease ending. Add `retire --unit U`, run at a unit's run end and on unit retirement. `review.sh` loses `--fresh` and `REVIEW_LEASE`, and `--lease` becomes `<slug>/<unit>`.
- `prime` re-points live reviewers' roster `instructions_file`. This needs a small `codex_mesh.py set-instructions --to N --instructions-file F`; `codex_mesh.py` is a shared module, so the full gate runs.
- Docs: `commands/unit/direct.md` (pool launch, <UXReview/>), `docs/delegate/dual_review.md`, `docs/delegate/phase_end.md`, `docs/production_format.md` and `commands/showrunner/produce.md` (no arm, no trial tables).
- Tests: review.sh pool path without an arm; re-prime keeps the reviewer and its thread; run end retires the unit's reviewers.

### Phase 3 — `/review:direct` shrinks, and the as-built
- `commands/review/direct.md` keeps curation and priming and drops health and the trial report. `review_pool_report.py` keeps its cost table over turn records, without the arm comparison.
- Rewrite `docs/as-built/review-pool.md` for the shipped design.
- `scripts/production/add_unit.py` is not expected to change. If it must, that change waits for herdr-trial Phase 10's merge.

## Risks
- A long-lived thread's context grows. The launcher already compacts above the configured limit, and Codex compaction keeps the thread.
- The fork in step 2 copies a busy reviewer's thread at its last completed turn, which is the context we want. A fork taken while the source turn is still running gets the state before that turn.
</content>
</invoke>
