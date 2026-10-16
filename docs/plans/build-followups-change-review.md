# Change review for every production

> **Status: IMPLEMENTATION PLAN — two checkpoints, build-report unit.** Every top-level showrunner keeps a change review list the user asks for on demand, generalized from Hana's.

> **Production: build-followups** — unit `build-report`; production doc `docs/plans/build-followups-production.md`

## Request (user via showrunner, 2026-10-15 ~18:15 EDT)

"I want you (and every top level showrunner) to start a changelog the way that hana does it - so you accumulate
changes for me to review and i ask for them on demand - they should not be part of the question routine - it should
probably be skill managed in order to add things to it - maybe by unit directors and showrunners - and the showrunner
should have a defined way to handle changes - probably tied into our feature log. then the /showrunner:accounts would
just be a standard change i would review"

Showrunner: generalize Hana's change review; non-visual changes included; entries through a skill; on-demand list and
verdicts; a verdict asking for a change goes back to the unit; never in open_for_user or the question count. First
entry: "/showrunner:accounts table: two 5-hour columns removed" (two-accounts, 216c4e1d).

## Decisions

- **Every production names its list.** `**Change review:**` becomes a required Production Context line;
  `/producer:to_production` writes `~/rust/hanadocs/change-review/<slug>.md` for a new production. Hana keeps
  `~/rust/hanadocs/change-review.md`. A missing file is created with the standard skeleton on its first row.
- **Rows still come from `Change-Review:` commit lines** through merge_checkpoint.py. A change that reaches the user
  without a checkpoint line (a showrunner's own change, a fix outside a run) goes in through the skill.
- **One skill, `/change_review`,** for units and showrunners: `add` (one row), `show` (the user asked: walk the
  To review rows one at a time), `verdict` (move a row to Reviewed). The list is found from the production doc: the
  showrunner's update timer, or a unit plan's Production line.
- **change_review.py** gains `add`, `show` and `verdict` actions and a lock shared with merge appends.
- **Never a question.** A change for the user to check is never put in open_for_user and never counted; it goes on
  the list. The user asks for "change review" when they want it.
- **Verdicts** (every production): `works`/`fine` lands the row; any other verdict drops it and sends the row and
  the verdict to the unit that made it; the fix returns as a new row.
- **Feature log.** A landed row adds a feature-log note when the production's project is one the feature log knows.
  ~/.claude becomes a feature-log project (`claude`, repo natepiano/claude_commands), so build-followups changes land
  as notes too. The vault folder `feature-log/claude/` and its Base are asked of hanadocs.

## Checkpoint 1 — the list everywhere

change_review.py (`add`, `show`, `verdict`, skeleton, lock) + tests; `commands/change_review.md`;
`docs/production_format.md` (required line, ProductionUnit entry rule); `commands/producer/to_production.md`;
`commands/showrunner/produce.md` (never an open question, verdicts for every production, return to the unit);
the build-followups production doc's Change review line.

## Checkpoint 2 — the feature log

feature_log.py `claude` project + tests; produce.md ChangeReviewFeatureLog runs for every feature-log project;
`docs/as-built/feature-log.md`; `/validate_and_push` close-out unaffected.
