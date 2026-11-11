# Change review for every production

> **Status: IMPLEMENTATION PLAN — two checkpoints, build-report unit.** Every top-level showrunner keeps a change review list the user asks for on demand, generalized from Hana's.


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

- **Every production names its list.** `**Change review:**` names `~/rust/hanadocs/change-review/change-review - <showrunner>.md` for its top-level showrunner.
  A child production names its root's list. The first row creates a missing list with the standard skeleton.
- **Rows still come from `Change-Review:` commit lines** through merge_checkpoint.py. A change that reaches the user
  without a checkpoint line (a showrunner's own change, a fix outside a run) goes in through the skill.
- **One skill, `/change_review`,** supports `add`, `show`, and `verdict` for units and showrunners.
  The shared scoped queue uses current staff ownership and includes earlier production lists.
  Both `/next change` and `show` use this queue. Earlier lists retain qualified row ids without copying their rows.
- **change_review.py** gains `add`, `show` and `verdict` actions and a lock shared with merge appends.
- **Never a question.** A change for the user to check is never put in open_for_user and never counted; it goes on
  the list. The user asks for "change review" when they want it.
- **Verdicts** land a row when they open with `works` or `fine`, after optional `okay` or `ok` and a row id.
  Case and later words do not change that result. Other verdicts return the row and verdict to its unit.
  The fix returns as a new row.
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
