# Proposals as issues — skills

> **Status: IMPLEMENTATION PLAN — two checkpoints, build-report unit.** The showrunner, unit and issue skills file, approve, start and close proposals as hanadocs issues instead of rows in `proposals/index.md`.

> **Production: build-followups** — unit `build-report`; production doc `docs/plans/build-followups-production.md`

## Decisions (mine, settled with the hanadocs dashboard unit 2026-10-15)

- **Lifecycle, four states.** Written = open/backlog, `plan` empty. Approved, not started = open/backlog with
  `showrunner`, `unit` and `plan` set. Being built = open/active (the unit sets active when its run starts).
  Built = closed with `as_built`. The spec's summary line says approval sets active; its migration rules and
  the vault's `proposals.base` views use the four states above, so the four states win.
- **Fields** are plain text: `proposed_by`, `showrunner`, `unit`, `plan` (`<plan path> Phase 4–6`),
  `as_built` (`docs/hana/as-built/<file>.md`). Tag `proposal` beside `issue`. Template placeholders are
  `{{PROPOSED_BY}}`, `{{SHOWRUNNER}}`, `{{UNIT}}`, `{{PLAN}}`, `{{AS_BUILT}}` (vault commit 47eb89ae).
- **Stars** (`backlog_goal`, `backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort`)
  are proposed together in the reply and written only after the user confirms or changes them.
- **One script edits issue frontmatter**: `scripts/production/proposal_issue.py` with verbs `approve`,
  `start`, `close` and `stars`. Skills call it rather than editing YAML by hand. Filing stays with `/issue`.
- **A plan names its issues** with an optional header line `> **Issues:** <issue path>[, <issue path>]`,
  preserved verbatim like the Production line. The end-of-run as-built pass closes every issue it names,
  with `as_built` set to the as-built doc the run wrote.
- **The production doc's `Proposals:` line** now names the issues folder (`~/rust/hanadocs/issues`).
  `/showrunner:proposals` lists every open issue there tagged `proposal`, across projects, as the old index
  did across production headings. The hana production doc's line is the hana showrunner's to change.
- **dailies counts** gain a kind `issues <folder> <tag>` counting open issues with that tag.
- **targets.md** is read "when present" by ux_eval, the UI review pool and the contract lens; a miss is a defect.

## Checkpoint 1 — the proposals table reads issues

- `proposals_table.py`: drop the index and the `docs/plans/proposal-*.md` sources; add the issues source
  (frontmatter: status open, tag proposal; state from stage/plan; proposer from `proposed_by`; date from
  `date_created`). Keep the backlog and add-on sources. Tests updated.
- `commands/showrunner/proposals.md`, `docs/production_format.md` (`Proposals:` line),
  `docs/as-built/showrunner-automation.md`.
- `dailies_counts.py` `issues` kind + test; `config/dailies.conf` hana line.

## Checkpoint 2 — the proposal lifecycle

- `proposal_issue.py` (+ test) and the new `/showrunner:proposal` skill (file via `/issue`, approve, close).
- `commands/issue.md`: tag proposal and the five new fields; propose all five stars in one reply, write after
  confirmation. `commands/close_issue.md`: keep `as_built`.
- Unit side: `<ProductionUnit/>` in `docs/production_format.md` and `produce.md` "Designs and add-ons" — a unit
  writes a proposal with `/issue` as `proposed_by`; on its run start it calls `start`.
- `docs/delegate_plan_format.md` Issues header, carried by to_phased_plan, to_production, phase_review, shrink.
- `docs/delegate/final_gate_commit.md` RunAsBuilt: read Issues before the plan is deleted, close each.
- targets.md reads: `commands/ux_eval.md`, `commands/review/direct.md`, `config/review_pool/ui.md`,
  `docs/delegate/dual_review.md` contract lens.

## Spec (verbatim, user-approved 2026-10-15)

# Proposals become issues — spec (user-approved 2026-10-15)

Approved by the user in the hana showrunner session. Implement exactly this.

## Schema (hanadocs `issue structure.md`, template, bases)
- A proposal is an issue in `~/rust/hanadocs/issues/` with tag `proposal`. `category` keeps its topical value (bug, business, docs, enhancement, example, feature, idea, refactor, research).
- New properties, valid on EVERY issue (not only proposals):
  - `proposed_by`: the unit or session that wrote it.
  - `showrunner`: the showrunner whose production builds it (set when work starts).
  - `unit`: the unit director building it (set when work starts).
  - `plan`: the plan doc path + phase numbers the work was written into (the unit writes the proposal's work into its plan doc as phases citing the issue).
  - `as_built`: link to the as-built doc (repo `docs/hana/as-built/<file>.md`) once it lands.
- Status/stage/ranking are the existing issue ones. Lifecycle: written → open/backlog; user approves → stage active + showrunner + unit + plan; last phase lands and its as-built is written → `as_built` set, status closed.
- `proposals.base` (filter: tag proposal) replaces `proposals/index.md`.
- Ranking stars: the user turned off interactive questions. Whoever files an issue or proposal PROPOSES all ranking stars (backlog_goal, alignment, impact, urgency, effort) at once in its reply; the user confirms or changes them in their reply. Never ask star by star.

## Migration (hanadocs)
- All 36 proposals (36 files + index.md under `~/rust/hanadocs/proposals/`; verify the count against index rows across every production heading) move to `issues/<slug>.md` with frontmatter.
- Supporting files:
  - Images (png, svg) → `conf/media/<slug>-<name>.<ext>`, linked from the issue.
  - Research notes (md) → `issues/<slug>/` beside the issue (user accepts that subfolder), linked from it.
  - arrange-jacks-widget's .txt is folded into its issue text.
- Back-populate from index.md rows:
  - Date → date_created.
  - Unit → proposed_by.
  - "waiting for review" → open, backlog.
  - "approved, not started: X Phase N" → open, backlog, unit + plan.
  - "being built by X" → open, active, showrunner + unit + plan.
  - Built with an as-built doc → closed + as_built.
  - Project from the production heading.
- Back-populate showrunner/unit on existing non-proposal issues only where known.
- Ranking stars for the 36: draft them, then hand the draft list to the hana showrunner, which shows it to the user to confirm.
- Cleanup:
  - Delete `proposals/index.md` and the emptied `proposals/` folder.
  - Rewrite every link to `hanadocs/proposals/...` in hanadocs, in the repo docs (hana_catalyst checkout and unit worktrees: report the files, don't edit unit worktrees), in `change-review.md`, `bevy-issues.md` and the showrunner handoff logs.

## Skills (~/.claude)
- Showrunner: one new skill to file a finished proposal as an issue (with proposed stars), record user approval with showrunner/unit/plan, and close it with as_built on landing. It replaces the "add a row to proposals/index.md" step everywhere (produce.md and others). `/showrunner:proposals` and proposals_table.py read the issues' frontmatter instead of index.md and the docs/plans/proposal-*.md scan (keep the backlog/add-on sources).
- Unit directors: no new skill.
  - A unit writes a proposal from the issue template, with proposed_by and proposed stars.
  - `/unit:direct`'s end-of-run as-built pass sets `as_built` (and closes) on the issue its plan names.
- Issue manager: learns the new fields and the star-proposal rule.
- Also (targets): the contract reviewer and the design check (ux_eval) read the repo's `docs/hana/targets.md` like `docs/design-decisions.md` and flag a miss as a defect, once that file exists.
- Memory to update after: proposals-accumulate-unshown (hana showrunner does its own).
