# Production rename

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A tool that renames a running production's slug everywhere it is stored, so old names keep resolving; first use renames `build-followups` to `natedev-mgmt`. Scope (author's choice): this plan ships the tool and its command doc; the showrunner runs the real rename with that command afterwards.

> **As-built disposition: create** — new `docs/as-built/production-rename.md`

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` scripts — production tooling under `scripts/production/`.
- **Project started:** 2026-10-15T08:34:01.905+00:00
- **Stack:** Python 3.13 stdlib only; zsh/bash launch scripts; git; tmux.
- **Layout:**
  - `scripts/production/` — showrunner and unit tooling; tests sit beside each script as `test_<name>.py`.
  - `commands/showrunner/` — showrunner command docs.
  - `docs/as-built/` — as-built docs.
- **Key files:**
  - `scripts/production/showrunners.py` — reads showrunners from update timers. `_timer` (line 171) takes the slug from the notifier instance name `showrunner-<slug>` (line 180). `production_slug(doc)` (line 222) takes it from the doc name minus `-production.md`. `SHOWRUNNER_STATE_DIR/<slug>/parent` holds the tree edges.
  - `scripts/production/add_unit.py` — launches units. Takes the slug from the doc name (line 272). Checks the Units row (lines 509-521: Plan, Worktree and Branch must match the request). Tmux session marks are set as `-e NAME=value` from the `marks` dict (lines 820-825); the `@claude_config_dir` and `@claude_model` options are set at line 830.
  - `scripts/production/tidy_wrapped.py` — `owned_paths(slug)` (line 30) is the canonical list of per-slug showrunner state: `footers-off/<slug>`, `review-paused/<slug>.json`, `outstanding/<slug>.json`, `<slug>/`. `SLUG` (line 22) is the slug pattern. `SHARED` lists the names a slug may never take.
  - `scripts/production/rename_state.py` — moves saved report state from one unit key to another, with atomic JSON and line rewrites (`_write_json`, `_write_lines`, `RenameRefused`). Reuse its writers; do not copy them.
  - `scripts/production/stall_watch.py` — `stretch_path(slug, unit)` (line 136) is `STALL_WATCH_STATE_DIR/sha256("<slug>\0<unit>").json`.
  - `scripts/production/merge_checkpoint.py` — `merge_history` (line 347) derives the last merge per unit from merge-branch subjects `Merge <unit> phase <N> (<hash>)` (regex at line 334). The subject holds no branch name, so the history survives a branch rename. There is no stored LAST_MERGED.
  - `scripts/production/log_backup.py` — backups at `SHOWRUNNER_STATE_DIR/<slug>/log-backups/` (line 61). They move with the `<slug>/` dir.
  - `scripts/delegate/review_pool.py` — the pool root is `REVIEW_POOL_STATE/<slug>` (lines 266-270), with leases written `<slug>/<unit>/<phase>`.
  - `scripts/production/waiting.py` — its search over production docs and units is the lookup to reuse for "which docs name this slug".
  - `scripts/berth_fix/launch.sh` — `relocate_legacy_worktree` moves a git worktree with `git worktree move` and repairs it; model the worktree move on it.
  - `scripts/production/test_production_lifecycle.py` — lines 77-180 build a scratch production: a repo, a merge branch, a doc, a notifier instance and a state dir under temp `HOME`-style env overrides. Model the rename fixture on it.
- **Every place a slug lives** (from a sweep of `scripts/`, `commands/`, `docs/` and the Claude config dirs, 2026-10-15):
  1. Production doc file name `docs/plans/<slug>-production.md`, plus the unit plans' `> **Production:** <slug>` header lines.
  2. Notifier instance dir `~/.local/state/notifier/showrunner-<slug>/` (its `conf` CHECK names the doc path).
  3. Showrunner state `~/.local/state/showrunner/{<slug>/, footers-off/<slug>, review-paused/<slug>.json, outstanding/<slug>.json}`, plus every other production's `<child>/parent` file whose content is `<slug>`.
  4. Stall-watch files `~/.local/state/stall-watch/sha256(slug\0unit).json`, one per unit.
  5. Review pool `~/.local/state/review-pool/<slug>/`.
  6. Merge branch `<slug>` (local and origin) and unit branches `<slug>-<unit-stem>` (local and origin), named in the Units table Branch cells.
  7. Unit worktrees `~/worktrees/<repo>-<slug>-<unit-stem>`, named in the Units table Worktree cells and in each worktree's git admin dir.
  8. Each unit's transcript dir `<CLAUDE_CONFIG_DIR>/projects/<path-with-dashes>` and its trust entry in `<CLAUDE_CONFIG_DIR>/.claude.json` `projects`, both keyed by the worktree path. Every config dir is found by `ls -d ~/.claude*/`.
  9. Tmux session marks and session names from `add_unit.py` (production slug mark), read by `unit_lookup.py`.
  10. Commit subjects (`merge(<slug>-…)`) and log lines. These are history and are never rewritten.
- **Test lanes:** `scripts/production/` — tests beside each script (`test_showrunners.py`, `test_rename.py`), run by unittest from the repo root.
- **Build:** none (Python).
- **Test:** from the repo root, `python3 -m unittest scripts.production.test_<name>`, and the gate `python3 scripts/production/merge_gate.py --repo <worktree> --tests-only`.
- **Lint:** `basedpyright <changed files>` — 0 errors, 0 warnings, no file-level ignores, no `Any` (user rule, CLAUDE.md).
- **Invariants:**
  - Never rewrite history: commit subjects and log lines keep the old slug. Source: user rule, never force-push.
  - Every write is atomic: temp file plus `os.replace`, and `git worktree move` for worktrees. Source: rename_state.py's existing practice.
  - `apply` refuses while a unit run is live in a worktree it would move. Source: the user's rule never to modify another session's claims; a moved cwd breaks a live session.
  - Old slugs resolve forever through the `**Formerly:**` line. Source: the showrunner's relay of the user's request, plus the unit messages already in flight.

## Phases

### Phase 1 — Formerly resolver  · status: done

#### Work Order

**Goal:** Every slug lookup accepts a former slug and answers for the production that now holds it.

**Depends on:** none.

**Spec:**
- A production doc may carry `- **Formerly:** \`<old>\`[, \`<older>\`]` in Production Context.
- `showrunners.py` gains `canonical_slug(slug: str, state_dir: Path | None = None) -> str`. It returns `slug` when a timer has it. Otherwise it reads each registered timer's doc for a Formerly line naming `slug` and returns that timer's slug. With neither, it returns `slug` unchanged: the resolver only maps former names, and each caller keeps its own existing check for an unknown production, so the review pool and unit lookup still work for a production with no timer. (Changed in phase 1 review: a raising resolver broke both for timer-less productions.)
- `current_name`, `current_address`, `named`, `attach`, `detach`, and the `parent|children|root` CLI all canonicalize their slug argument first.
- `production_slug(doc)` is unchanged; the doc name is the current slug.
- `unit_lookup.py pane|list <slug>` and `review_pool.py`'s production argument canonicalize the same way, by importing `canonical_slug`.

**Files:**
- `scripts/production/showrunners.py` — `canonical_slug`, plus its uses.
- `scripts/production/unit_lookup.py` — canonicalize its slug argument.
- `scripts/delegate/review_pool.py` — canonicalize at lines 266-270.
- `scripts/production/test_showrunners.py` — tests.

**Workers:** 1 writer + 1 tester
- `impl` — showrunners.py, unit_lookup.py, review_pool.py; hub: `scripts/production/showrunners.py` (every lookup imports it).
- `test` — test_showrunners.py: a timer for `new` whose doc says Formerly `old`. `address old` returns new's address. An unknown slug passes through `canonical_slug` unchanged and `address` still refuses it. A slug both current and former resolves to the current one.

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest scripts.production.test_showrunners scripts.production.test_unit_lookup scripts.delegate.test_review_pool` green; basedpyright 0/0.

### Phase 2 — rename.py plan|apply (production-wide)  · status: todo

#### Work Order

**Goal:** `rename.py plan <old> <new>` prints every move in items 1-6 of the slug list without touching anything. `apply` performs them atomically and stops at the first refusal.

**Depends on:** Formerly resolver.

**Spec:**
- `new` must match `tidy_wrapped.SLUG`, must not be in `SHARED`, and must not already be a timer slug or a Formerly entry.
- Plan items, in apply order:
  1. `git mv` the doc to `<new>-production.md` and add or extend `**Formerly:**`.
  2. Rewrite every unit plan's Production header.
  3. Rename the notifier instance dir and rewrite the doc path in its `conf` CHECK.
  4. Rename the four showrunner state paths and rewrite every `parent` file containing `<old>`.
  5. Rename each stall-watch file `sha256(old\0unit)` to `sha256(new\0unit)`, for each Units row.
  6. Rename the review-pool dir.
  7. Create the merge branch `<new>` at `<old>`'s tip, push it, and keep `<old>` until `finish`. Rewrite the Merge branch line.
- The doc and plan edits land as one commit on the merge branch, in the showrunner's checkout: `chore(<new>): rename production from <old>`.
- `apply` writes a journal `SHOWRUNNER_STATE_DIR/<new>/rename.json` (step, done flag), so a rerun resumes.
- All paths are env-overridable as in showrunners.py, so the tests run against temp dirs.

**Files:**
- `scripts/production/rename.py` — new: `plan`, `apply`.
- `scripts/production/test_rename.py` — new.

**Workers:** 1 writer + 1 tester
- `impl` — rename.py; hub: `scripts/production/rename.py`.
- `test` — test_rename.py, on a fixture modeled on test_production_lifecycle.py:77-180: plan prints every item; apply moves every one; a rerun after a simulated failure at step 4 resumes; refusals for a bad or taken slug.

**Constraints from prior phases:** `canonical_slug` resolves Formerly entries. `apply` must add the Formerly line before renaming the timer dir, so lookups never fail in between.

**Acceptance gate:** `python3 -m unittest scripts.production.test_rename` green; basedpyright 0/0.

### Phase 3 — rename.py unit|finish (per unit)  · status: todo

#### Work Order

**Goal:** `rename.py unit <new> <unit>` moves one unit's branch and worktree between its runs. `finish <new>` retires the old merge branch once every unit has moved.

**Depends on:** rename.py plan|apply.

**Spec:**
- `unit` refuses while the unit's tmux session has a live Claude in the worktree; the check uses `unit_lookup.py pane`.
- Branch: create `<new>-<stem>` at the old tip, push it, and delete the old local and remote branch only after the push succeeds.
- Worktree: `git worktree move` it to the new path, then `git worktree repair`, as in `relocate_legacy_worktree`.
- For each `~/.claude*/` config dir holding them: rename `projects/<old-path-dashed>` to `projects/<new-path-dashed>`. In `.claude.json` `projects`, copy the old worktree path's entry to the new path's key and leave the old key in place, writing atomically with rename_state's writers. Live sessions on that account rewrite the whole file from memory, so read it back after 2 s and write again if the new key is gone, up to 3 tries. After that, refuse and name the file; the unit then gets one trust prompt at its next launch, which is the only cost.
- Rewrite the unit's Units row Worktree and Branch cells in the same commit style as Phase 2.
- The tmux mark is rewritten when the unit is next launched by add_unit; `unit` prints that.
- `finish` refuses while any Units row still names an `<old>`-prefixed branch. It then deletes the `<old>` merge branch locally and on origin and marks the journal done.

**Files:**
- `scripts/production/rename.py` — `unit`, `finish`.
- `scripts/production/test_rename.py` — tests.

**Workers:** 1 writer + 1 tester
- `impl` — rename.py.
- `test` — test_rename.py: a unit move on a temp repo with a worktree, and a temp config dir holding a projects dir and a `.claude.json` key; refusal while live; finish refuses while a row is unmoved.

**Constraints from prior phases:** the journal at `SHOWRUNNER_STATE_DIR/<new>/rename.json`; Formerly resolution; Phase 2 keeps the old merge branch until `finish`.

**Acceptance gate:** `python3 -m unittest scripts.production.test_rename` green; basedpyright 0/0.

### Phase 4 — Command doc and as-built  · status: todo

#### Work Order

**Goal:** The showrunner can run a rename from a command doc, and the as-built records it.

**Depends on:** rename.py unit|finish.

**Spec:**
- `commands/showrunner/rename.md`: the order is plan, then apply, then for each unit between its runs `unit`, then `finish`. Tell each unit director its new worktree path before it resumes. Never rewrite history.
- Add the `**Formerly:**` line to `~/.claude/docs/production_format.md`'s doc structure, in the Production Context list.
- `docs/as-built/production-rename.md`.

**Files:** `commands/showrunner/rename.md`, `docs/production_format.md` (outside this unit's Units row; send the one-line overlap to the showrunner before editing), `docs/as-built/production-rename.md`.

**Workers:** 1 writer; nothing splits (docs only).
- `impl` — all three files.

**Constraints from prior phases:** the CLI verbs and refusals as built in the earlier phases.

**Acceptance gate:** the lint suite (`python3 -m unittest discover scripts/lint`) green; the doc names only verbs that exist.

### Phase 5 — Live check on a scratch production  · status: todo

#### Work Order

**Goal:** A real rename of a scratch production on natedev, end to end, leaves every lookup working under both names.

**Depends on:** Command doc and as-built.

**Spec:**
- Build a scratch production with one unit by hand, as test_production_lifecycle.py:77-180 does: a scratch git repo whose origin is a local bare repo, a doc, a Units row, a worktree, and state dirs pointed at a scratch root through the env overrides. Run the real `rename.py` CLI: plan, apply, unit and finish. No branch reaches GitHub. Check `showrunners.py address <old>` and `<new>`, `unit_lookup.py list <old>`, the stall watch and the review-pool lease paths.
- Then delete the scratch root, which holds every path the check created.
- Never touch a real production's state.

**Files:** none changed; a findings note goes into the handoff only.

**Workers:** 1 writer; nothing splits.
- `impl` — runs the check and reports.

**Constraints from prior phases:** all of the above.

**Acceptance gate:** every lookup answers under both slugs; the scratch state is removed afterwards.
