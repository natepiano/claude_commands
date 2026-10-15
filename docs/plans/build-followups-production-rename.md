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

#### As-built

- `rename.py unit <new> <unit>` and `finish <new>` run only once the journal (`rename.json`) shows apply step 7, and hold `rename.lock` while they run. `unit` moves one stopped unit through journal steps 0–5 in `unit_moves[<unit>]`. The steps: push `<new>-<stem>` at the recorded old tip; `git worktree move` it to `<repo>-<new>-<stem>`, then repair and switch to the new branch; move `projects/<dashed path>` in every `~/.claude*` dir and in `CLAUDE_CONFIG_DIR`, and copy the trust key in `.claude.json` (2 s read-back, 3 tries, then refuse and name the file); rewrite the unit's Units row Worktree/Branch cells and commit as `chore(<new>): rename unit <unit> from <old>`; delete the old branch on origin, then locally. A rerun resumes from the recorded step. The command prints that add_unit rewrites the tmux mark at the next launch.
- `unit` refuses when: Claude is live or its state is unknown (`LiveClaude`/`ClaudeUnknown`); any session host is unreadable (`marked_units(new, every_host=True)`); the row is retired or missing a Worktree/Branch column; the old branch has moved past the recorded tip; another unit's move is unfinished; the rename is already done; the showrunner checkout is dirty or not on the new merge branch.
- `finish` refuses while a non-retired row names an `<old>-` branch, a unit move is unfinished, or the checkout is off the new merge branch. It deletes the journal's `merge_branch` (which may differ from `<old>`) on origin and then locally, records `finish_step` 1–2, and sets `done: true`. A rerun after success does nothing.
- `unit_lookup` files sessions that carry old-slug marks under the current slug, through a private `_lookup_slug`. A slug whose `showrunner-<slug>` notifier dir exists is current and needs no timer-doc read. Any other slug goes through `showrunners.canonical_slug`, once per distinct discovered slug. Duplicate detection still applies after normalization, so `list` and `pane` find a unit under either slug until it is relaunched.
- `marked_units(slug, *, every_host=False)` / `every_marked_unit(*, include_tmux=True, every_host=False)`: on the default path a single host failure is tolerated, and the call raises only when every host fails, with each error's original text. `every_host=True` raises on any host failure, and adds the host name as a prefix only when the error text does not already start with it.
- `stall_watch` matches detached work by canonical slug, so work with old-slug marks still counts.

**Files:**
- `scripts/production/rename.py` — `unit`, `finish` and their CLI subcommands.
- `scripts/production/unit_lookup.py` — canonical-slug discovery via `_lookup_slug`, `every_host`.
- `scripts/production/stall_watch.py` — canonical matching of process marks.
- `scripts/production/test_rename.py`, `test_unit_lookup.py`, `test_stall_watch.py` — tests.

**Binds later work:** Command doc and as-built documents the refusals listed above and states that recovery is a rerun of the same command (unit move or finish). Live check on a scratch production reaches a worktree path containing `.`/`_`, and both trust-file layouts, only through the `HOME`/`CLAUDE_CONFIG_DIR` overrides.

**Gotchas:**
- Claude's projects-dir name replaces every non-alphanumeric character of the worktree path with `-`, not only `/`.
- The default account's trust file is `~/.claude.json` in `HOME`, not inside `~/.claude/`. Other config dirs keep `<dir>/.claude.json`.
- A single failing session host can hide live units, so `unit` uses `every_host=True`.
- The old branch tip is re-checked before each destructive step.
- Commit detection reads the Units row at `HEAD`, so merges onto the merge branch between steps do not block a resume.
- `_lookup_slug` keeps the shutdown inventory within its timer-read budget: the membership check and the missing-target check share one timer read.
- `unit_lookup.py` is shared: the shutdown (`scripts/shutdown/test_inventory.py`) and message (`scripts/message/test_roster.py`) suites exercise it. A change there needs the full merge-gate suites (`scripts/production/merge_gate.py --tests-only`), not only the production suites.

### Phase 4 — Command doc and as-built  · status: done

#### As-built

- `/showrunner:rename` runs `plan`, `apply`, then `unit <new> <unit>` for each non-retired row between its runs, then `finish`. It lists the refusals, says apply finishing is not the end (the old merge branch stays until `finish`), and recovers by rerunning the same command with the destination fixed; the source accepts the old, new or a former name.
- The showrunner tells each unit director the new worktree path before it resumes. No rename force-pushes or rewrites history.
- `rename.lock` is internal coordination; `rename-history/` is archival state, not a recovery path.
- The as-built covers files and readers, Formerly resolution with the current-name fast path, apply steps 0–7, unit steps 0–5, account project encoding and trust locations, journal and recovery, finish, exit codes, state roots and tests, and the full merge-gate run required for `unit_lookup.py` changes.

**Files:**
- `commands/showrunner/rename.md` — showrunner workflow and recovery instructions.
- `docs/as-built/production-rename.md` — implementation record.
- `docs/production_format.md` — Production Context's `**Formerly:**` line.

**Binds later work:** Live check on a scratch production points `HOME` and `CLAUDE_CONFIG_DIR` at the scratch root along with the state dirs, since `unit` moves `projects/` dirs in every `~/.claude*` dir and writes trust into `~/.claude.json`. `unit` uses strict every-host discovery, so session fixtures need `UNIT_LOOKUP_TMUX`, `NOTIFIER_SESSIONS_DIR` and a herdr fake.

**Gotchas:** The command doc names no internal journal step numbers; `unit` and `finish` require `apply` to have finished through its merge-branch push.

### Phase 5 — Live check on a scratch production  · status: done

#### As-built

- A scratch production on natedev (local bare origin, isolated account and state roots, fake tmux and herdr discovery) completes `rename.py` plan, apply, unit and finish with no branch reaching GitHub.
- `showrunners.py address`, `unit_lookup.py list` and `pane` give identical non-empty answers under the old and new names after apply, before and after the unit move, and after finish; canonicalized stall-watch and review-pool lease paths resolve to the same retained files under both names.
- A worktree path holding `.` and `_` moves, transcripts move, and trust entries copy in both the default and the separate account layouts.
- The scratch root holds every path the check creates and is removed afterwards; no real production's state is touched.

**Files:**
- `docs/as-built/production-rename.md` — Live check section recording what the scratch run shows.

**Gotchas:** A rerun needs `HOME`, `CLAUDE_CONFIG_DIR`, `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR`, `STALL_WATCH_STATE_DIR`, `REVIEW_POOL_STATE`, `NOTIFIER_SESSIONS_DIR`, `UNIT_LOOKUP_TMUX` and the herdr fakes (`HERDR_BIN`, `HERDR_PROC`) all inside the scratch root; with the real `HOME`, `unit` writes the user's account files. `unit` discovers sessions with `every_host=True`, so both hosts must answer, and the marked unit must read as not running (`ClaudeNotRunning`) for the move to proceed. Git needs explicit identities, `GIT_CONFIG_NOSYSTEM=1` and signing off; an `env -i` environment must carry the git and Python executable directories on `PATH`. A `unit_lookup.py` change also needs the shutdown and message suites, which exercise it. The check does not cover a real Claude process, a real tmux or herdr server, or a network git remote.
