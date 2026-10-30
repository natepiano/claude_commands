# Unit transfer (as built)

Code reviews now use the unit's persistent `review` and `review2` workers
through `assign.sh --role review`. Any pool maintenance paths described below
remain only for the pending archive cleanup; they do not dispatch new reviews.

`/showrunner:transfer_unit <unit> <to production doc>` (`commands/showrunner/transfer_unit.md`)
moves one natedev unit director from one production to another. The giving showrunner runs it. The
script is `scripts/production/transfer_unit.py`, with three actions: `plan`, `apply` and `verify`
(user, 2026-10-21: "yes i want this"). The first use is hierarchies-unit from build-followups to sdlc.

## What stays and what moves

- The unit keeps its id, its Claude session and conversation, its worktree, its branch and its
  unmerged commits.
- The Units row moves from the giving doc to the receiving doc. It is written under the receiving
  doc's own columns, and a column the giving row lacks gets `—`. The row is not marked retired.
- The plan's `> **Production:**` header names the receiving production and doc. The unit reads its
  production from that header, so the header commit goes on the unit branch, and the branch is
  pushed. The plan's prose is history and does not change. Work in progress stays: the commit is
  built from HEAD's plan in a temporary index (`commit-tree`, then `update-ref`), and the same header
  change then goes into the real index and the worktree file (showrunner, 2026-10-21).
- The stall-watch stretch and its prompt file move to the receiving slug. This pool's reviewers of
  the unit retire (`review_pool.py retire`), so the receiving pool leases its own. The old pane
  entry is forgotten, because the relaunch records a new pane.
- Each held design the unit holds (`scripts/guilds/roles.py`) takes the receiving production as
  holder (`roles.move_holder`), so resume, rebind and end accept the relaunched unit. The role
  threads, role names, checkout paths and saved prompts stay.
- The shutdown record's host takes the receiving `production` and `doc`. A herdr session name of
  the form `<giving slug>-<stem>` takes `<receiving slug>-<stem>`. Its `renamed` field holds the
  moved note, which `restart.restart_note` puts first in the relaunched session's prompt. The note
  names the new production doc, merge branch and showrunner address command.
- The relaunch runs `add_unit.py` with the record's doc, so the unit's marks
  (`SHOWRUNNER_UNIT`, `SHOWRUNNER_UNIT_ID`) name the receiving production. Status, dailies, panes and
  merges find the unit there.
- A held design role (`docs/as-built/long-lived-workers.md#lasting-roles`) does not move: `transfer_unit.py` calls no
  `roles.py` verb, so its `UnitHolder` keeps the giving slug. After the relaunch `resolve_holder`
  reports `unit holder is not recorded`, and every `roles.py` verb that authorizes the holder
  (`resume`, `rebind`, `end`) refuses.
- The phase note follows at the unit's next refresh. The giving showrunner's report state keeps its
  rows as history.

## Refusals (version one)

- A unit on the Mac, or a receiving production in another repository (another git common dir):
  the worktree could not be kept.
- A merge in progress (`MERGE_HEAD`) in the unit worktree or in either showrunner checkout.
- Commits on the unit branch that come from the giving merge branch and that the receiving merge
  branch does not have (`foreign_commits`): they would move with the unit.
- A reserved or retired unit, a unit already in the receiving table, and an unfinished rename or
  transfer in either production.
- `plan`, and a fresh `apply` before its stop, also refuse what a later step refuses: a checkout off
  its merge branch or with doc edits, what the stop step cannot settle, and an origin unit branch
  the push would not fast-forward. A refused transfer never stops the unit.

## apply

apply works under the production lock in `unit_move` and saves the journal `transfer_unit.json` in
the giving production's state folder before it changes anything. A rerun resumes at the step that
refused. The journal keeps the request and progress, but not the productions' checkout or doc
paths. Resume and verify find the giving doc through its current update timer and read the
receiving doc supplied on the command line, just as a fresh apply does. Both checkouts come from
those docs. Old journals' stored locations are ignored, so a checkout move does not strand an
open transfer; the receiving doc must still name the journal's receiving production.
Each step except stop, relaunch and verify runs under both merge locks, taken in slug order,
and runs the preflight again first. Stop is `rename_unit.stop_unit` through the shared
`rename_unit.StopTarget` protocol: a transfer keeps the id, so the old and new ids are the same.

The doc commits use `git commit --only` on the doc, and an earlier run's commit is adopted by its
subject. If a run stops between a row write and its commit, the journaled step accepts only
its exact row change computed from HEAD (with the index still at HEAD or that same change),
then finishes the commit; unrelated document edits still refuse. They are not pushed: each goes out with its showrunner's next merge push, as add_unit's
row commit does (user, 2026-10-08). The unit takes the receiving merge branch at its own next merge.

## verify

verify fails when the receiving doc has no row, the giving doc still has one, a plan header does
not name the receiving production, the stretch is still under the giving slug, the shutdown record
does not name the receiving production, the unit is not live under the receiving slug, a session
still carries the giving marks, a giving reviewer is still leased to the unit, or a held design
still names the giving production as the unit's holder (`roles.holder_failures`).
