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

#### As-built

- A production doc may carry `- **Formerly:** \`<old>\`[, \`<older>\`]` inside its `## Production Context` section; `production_slug(doc)` is unchanged, so the doc name is the current slug.
- `showrunners.canonical_slug(slug: str, state_dir: Path | None = None) -> str` returns `slug` when an update timer has it; otherwise it returns the slug of the current production whose Production Context Formerly line names `slug`; otherwise it returns `slug` unchanged. It never raises. A slug that is both current and former resolves to the current one.
- `current_name`, `current_address`, `named`, `attach`, `detach`, and the `parent|children|root` CLI canonicalize their slug first. Each caller keeps its own existence check: `attach`, `detach` and the CLI still refuse a slug with no update timer.
- `named()` prefers the production a name resolves to over a live session that shares the name.
- `unit_lookup.py pane|list <slug>` and `review_pool.py`'s `_production` canonicalize the production slug by importing `canonical_slug`.

**Files:**
- `scripts/production/showrunners.py` — `canonical_slug`, Formerly parsing, resolved lookups.
- `scripts/production/unit_lookup.py` — canonicalizes the production argument.
- `scripts/delegate/review_pool.py` — canonicalizes the production argument.
- `scripts/production/test_showrunners.py` — resolver, collision and section-scoping tests.

**Binds later work:** `canonical_slug` reads Formerly only from `## Production Context` and returns an unknown slug unchanged; the rename tool's apply step must add the Formerly line inside Production Context before the update-timer dir moves (consumer: "rename.py plan|apply").

**Gotchas:** `test_unit_lookup` and `test_review_pool` use productions with no update timer, so a strict lookup at those callers breaks them. A `**Formerly:**` line outside `## Production Context` is ignored. The CLI `attach`/`detach` canonicalize twice (CLI and function); harmless.

**Ruled out:** a resolver that raises on an unknown slug — it breaks the review pool and unit lookup for timer-less productions. Importing `unit_lookup` as a package from the repo root — every importer loads production modules flat from `scripts/production`.

### Phase 2 — rename.py plan|apply (production-wide)  · status: done

#### As-built

- `rename.py plan <old> <new>` builds a `RenamePlan` (frozen dataclass of `PathMove(source, destination)` moves) and prints every move without writing.
- `rename.py apply <old> <new>` runs seven steps in order under `rename.lock`: (1) add or extend `**Formerly:**` as the first bullet of `## Production Context`, point the notifier `conf` CHECK at the new doc, `git mv` to `<new>-production.md`; (2) rewrite every unit plan's Production header (both header forms, plus the doc path); (3) rename the notifier instance dir; (4) move the four showrunner state paths and rewrite every `parent` file naming `<old>`, keeping any prior journal as `rename-history/<old>.json`; (5) move stall-watch files `sha256(old\0unit)` → `sha256(new\0unit)` per Units row; (6) move the review-pool dir; (7) create branch `<new>` at the old merge branch's tip, rewrite the Merge branch line, commit `chore(<new>): rename production from <old>` (new doc, old doc deletion, plan edits) and push with upstream.
- Refusals: bad or `SHARED` slug; `new` already a current or former slug; `old` without an update timer; checkout not on the merge branch or not clean; an existing local or origin `<new>` branch; any existing destination; an unfinished prior rename; the merge branch moving mid-rename; unrelated checkout changes.
- A rerun resumes after the last recorded step and accepts the source, the destination, or any former name resolving to either.

**Files:**
- `scripts/production/rename.py` — `plan` and `apply`, `RenamePlan`, `PathMove`.
- `scripts/production/test_rename.py` — fixture production with a bare origin; plan, apply, resume and refusal tests.

**Binds later work:** for "rename.py unit|finish": the journal `SHOWRUNNER_STATE_DIR/<new>/rename.json` (version 1, every move, `step`, `done`) is left at step 7 with `done: false` by apply and set done by finish; the old merge branch `<old>` is kept until finish; `RenamePlan.merge_branch` comes from the doc's Merge branch line and may differ from the old slug; retired Units rows contribute units but no plan paths.

**Gotchas:**
- Retired Units rows (Plan cell starting `(retired`) feed stall-watch moves but never a plan path, whatever their prose quotes (a backticked `/unit:report` is not a path).
- CHECK names the new doc before `git mv`, so the timer always names a readable doc; CHECK is rewritten by shell-parsed token (the argument after `production_check.sh`) and re-quoted with `shlex.quote`, since it can hold a shell-escaped path.
- A retry after a `git mv` that moved the file but not the index removes the old doc from the index explicitly.
- Every path honors the showrunners.py env overrides; never run against live state.

### Phase 3 — rename.py unit|finish (per unit)  · status: done

#### Work Order

**Goal:** `rename.py unit <new> <unit>` moves one unit's branch and worktree between its runs. `finish <new>` retires the old merge branch once every unit has moved.

**Depends on:** Phase 2

**Spec:**
- `unit` refuses while the unit's tmux session has a live Claude in the worktree, or when its state cannot be told: it inspects unit_lookup's `LiveClaude | ClaudeNotRunning | ClaudeUnknown` result (the `pane` command prints a pane in every state, so its output alone is not a liveness check), refuses `LiveClaude` and `ClaudeUnknown`, and proceeds on `ClaudeNotRunning`.
- `unit` and `finish` start only once the journal shows step 7; each records its own progress in the journal under the existing `rename.lock`, keeps the `done` flag it read, and resumes an interrupted unit move or branch deletion on rerun. `finish` run again after success is a no-op.
- Session discovery keeps working while sessions still carry old-slug marks: `unit_lookup.py` normalizes each discovered session's production through `canonical_slug` before grouping, keeping duplicate detection, so `list` and `pane` find a unit under either slug from apply until the unit is relaunched.
- Branch: create `<new>-<stem>` at the old tip, push it, and delete the old local and remote branch only after the push succeeds.
- Worktree: `git worktree move` it to the new path, then `git worktree repair`, as in `relocate_legacy_worktree`.
- For each `~/.claude*/` config dir holding them: rename `projects/<old-path-dashed>` to `projects/<new-path-dashed>`. In `.claude.json` `projects`, copy the old worktree path's entry to the new path's key and leave the old key in place, writing atomically with rename_state's writers. Live sessions on that account rewrite the whole file from memory, so read it back after 2 s and write again if the new key is gone, up to 3 tries. After that, refuse and name the file; the unit then gets one trust prompt at its next launch, which is the only cost.
- Rewrite the unit's Units row Worktree and Branch cells in the same commit style as Phase 2.
- The tmux mark is rewritten when the unit is next launched by add_unit; `unit` prints that.
- `finish` refuses while any Units row still names an `<old>`-prefixed branch. It then deletes the journal's recorded `merge_branch` (read from Production Context; it may differ from `<old>`) locally and on origin and marks the journal done.

**Files:**
- `scripts/production/rename.py` — `unit`, `finish`.
- `scripts/production/unit_lookup.py` — canonical production for discovered sessions.
- `scripts/production/test_rename.py` — tests.
- `scripts/production/test_unit_lookup.py` — tests.

**Workers:** 1 writer + 1 tester
- `impl` — rename.py and unit_lookup.py; hub: rename.py.
- `test` — test_rename.py and test_unit_lookup.py: a unit move on a temp repo with a worktree, and a temp config dir holding a projects dir and a `.claude.json` key; refusal while live and while unknown, success while stopped; finish refuses while a row is unmoved; retries of an interrupted unit move and finish; repeated finish; finish with a merge branch that differs from the slug; a later rename archiving the completed journal; list and pane finding an old-marked session under both slugs.

**Constraints from prior phases:** the journal at `SHOWRUNNER_STATE_DIR/<new>/rename.json` (apply leaves it at step 7 with `done: false`; `finish` sets `done: true`); Formerly resolution; Phase 2 keeps the old merge branch until `finish`. A retired Units row (Plan cell starting `(retired`, `add_unit.plan_cell_is_retired`) has no live worktree: `unit` refuses it, and `finish` ignores retired rows when it checks for unmoved `<old>`-prefixed branches. The live build-followups doc has such rows.

**Acceptance gate:** `python3 -m unittest scripts.production.test_rename scripts.production.test_unit_lookup` green; basedpyright 0/0.

### Phase 4 — Command doc and as-built  · status: todo

#### Work Order

**Goal:** The showrunner can run a rename from a command doc, and the as-built records it.

**Depends on:** Phase 3

**Spec:**
- `commands/showrunner/rename.md`: the order is plan, then apply, then for each unit between its runs `unit`, then `finish`. Tell each unit director its new worktree path before it resumes. Never rewrite history. Say that apply finishing is not the end (the old merge branch stays until `finish`); that a refused or interrupted step is retried by rerunning the same command, which accepts the old name, the new name, or a former name; and that `rename.lock` is internal coordination and `rename-history/` is archival state.
- Add the `**Formerly:**` line to `~/.claude/docs/production_format.md`'s doc structure, in the Production Context list.
- `docs/as-built/production-rename.md`.

**Files:**
- `commands/showrunner/rename.md` — the command doc.
- `docs/production_format.md` — the **Formerly:** line only (outside this unit's Units row; the showrunner granted it for this run on 2026-10-15).
- `docs/as-built/production-rename.md` — the as-built.

**Workers:** 1 writer; nothing splits (docs only).
- `impl` — all three files.

**Constraints from prior phases:** the CLI verbs and refusals as built in the earlier phases.

**Acceptance gate:** the lint suite (`python3 -m unittest discover scripts/lint`) green; the doc names only verbs that exist.

### Phase 5 — Live check on a scratch production  · status: todo

#### Work Order

**Goal:** A real rename of a scratch production on natedev, end to end, leaves every lookup working under both names.

**Depends on:** Phase 4

**Spec:**
- Build a scratch production with one unit by hand, as test_production_lifecycle.py:77-180 does: a scratch git repo whose origin is a local bare repo, a doc, a Units row, a worktree, and state dirs pointed at a scratch root through the env overrides. Run the real `rename.py` CLI: plan, apply, unit and finish. No branch reaches GitHub. Create isolated session fixtures (a running showrunner and a marked unit) so lookups have something to find. Check that `showrunners.py address` and `unit_lookup.py list` give the same non-empty answer under `<old>` and `<new>` after apply, between apply and the unit move, and after finish, plus the stall watch and the review-pool lease paths.
- Then delete the scratch root, which holds every path the check created.
- Never touch a real production's state.

**Files:**
- `docs/as-built/production-rename.md` — a short Live check section with what the scratch run showed; the run itself writes only under a scratch root it deletes.

**Workers:** 1 writer; nothing splits.
- `impl` — runs the check and reports.

**Constraints from prior phases:** all of the above.

**Acceptance gate:** every lookup answers under both slugs; the scratch state is removed afterwards.
