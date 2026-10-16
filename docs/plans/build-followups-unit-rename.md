# Unit rename: `/showrunner:rename_unit`

> **Production:** build-followups (enh-showrunner-unit)

## Why

There is no command that renames a unit. `/showrunner:rename` renames a production and moves
each unit's branch and worktree to the new production prefix (`scripts/production/rename.py`,
verb `unit`); it never changes a unit's own name. The user asked for
`/showrunner:rename_unit old new` on 2026-10-07; it was dropped when session names became
"looked up when needed, written down nowhere" (checkpoint 29a7b1f), which fixed only the
session-name half. The unit-name half is this plan. Source: the proposal
`~/.local/state/plan-backlog/2026-10-16-unit-rename.md` (a natedev subagent, 2026-10-16), approved
by the user with its four phases as proposed; the showrunner assigned it to this unit 2026-10-16.

## Decisions (the user, 2026-10-16)

1. **A renamed unit's session takes the new name.** The relaunch names the session after the new
   stem, as a new launch does; `/rename` still works afterwards.
2. **The first real use fixes the doubled names** `test-threads-unit-unit` and
   `feature-log-unit-unit` (both runs done), which got a doubled suffix before `add_unit.py`
   started stripping a given `-unit`.

## A unit's names

A unit has four names. Only one of them is its identity:

| Name | Example (build-followups) | What it is | Where it comes from |
| --- | --- | --- | --- |
| Unit id | `enh-showrunner-unit` | The key. "The Unit value names the unit everywhere" (`docs/production_format.md:135`) | Units row, Unit cell |
| Stem | `enh-showrunner` | Unit id less `-unit`. At launch it builds the branch, worktree and first session name (`add_unit.py:303-318`) | Derived once, then stored in the Branch and Worktree cells |
| Session name | `showrunner-fixer` | A label. You can `/rename` it any time; no script stores it | Claude's own session record |
| Plan file | `build-followups-herdr-adoption.md` | Names the **work**, not the unit. One unit runs many plans in turn | Plan cell |

## Every place a unit's name lives

Forms: **id** = unit id, **br** = branch, **wt** = worktree path, **sess** = session
name. "Today" says what a hand rename of the Units row alone would do.

### In the production checkout and git

| # | Place | Form | Writer | Reader | Today |
| --- | --- | --- | --- | --- | --- |
| 1 | Units row, Unit cell | id | `add_unit.py:391-394`, `adopt.py` | every production script via `production_units.unit_table` | the source |
| 2 | Units row, Worktree and Branch cells | wt, br | `add_unit.py:391-394`, `rename.py:668` | `merge_checkpoint.py:152`, `waiting.py:359`, `usage_report.py:337`, `rename.py:561` | breaks: point at paths that no longer exist |
| 3 | Hub files table, Owner and Other cells | id | to_production, by hand | `merge_checkpoint.py:478-491` (substring match) | breaks: hub overlap check misses the unit |
| 4 | Gates table, Waiting and Waits-on cells | `<id> phase N` | to_production, showrunner | `merge_checkpoint.py:530` | breaks open gates; cleared gates are history |
| 5 | Unit's live plan files: worktree path and branch baked into Work Orders | wt, br | plan:to_phased_plan, `add_unit.py:657-683` prompt rule | the director and its Codex workers | breaks: workers sent to the old path |
| 6 | Other rows' Owns prose ("owner followups-unit") | id | by hand | people | stale label |
| 7 | Merge commit subjects on the merge branch, `Merge <id> phase N (<sha>) into <mb>` | id | `merge_checkpoint.py:539-543`, `merge_gate.py:484` | `merge_checkpoint.py:333-347` → LAST_MERGED, first-merge gates, has_code_merge; `produce.md:37` | **breaks**: renamed unit looks never merged; first-merge gates fire again |
| 8 | Production log `docs/plans/<slug>-log.md` | id | `merge_checkpoint.py:850`, showrunner | people | history |
| 9 | Checkpoint subjects `checkpoint(<br>): …` on the unit branch | br | `/unit:checkpoint` | `usage/outcomes.py:317`, `change_review.py:142` (map by phase, not name) | history |
| 10 | Local and origin branch ref | br | `add_unit.py:636`, first checkpoint push | git, merge notices | must change |
| 11 | `branch.<br>.remote/.merge` | br | `push --set-upstream` | git | reset by the push |
| 12 | `branch.<br>.cargoBerthTarget` (repos with `berth.toml`) | br | `add_unit.py:638-642` | cargo-berth | breaks silently; **`rename.py` does not copy it either** |
| 13 | Worktree directory and `.git/worktrees/<name>` | wt | `add_unit.py:636` | git, everything | must change (`git worktree move` + `repair`) |
| 14 | Unit plan header `> **Production:**` | production only | to_production | `/unit:direct` | unaffected |

### Sessions

| # | Place | Form | Writer | Reader | Today |
| --- | --- | --- | --- | --- | --- |
| 15 | Process env marks `SHOWRUNNER_UNIT` (slug) and `SHOWRUNNER_UNIT_ID` | id | `add_unit.py:711-712`, `unit_lookup.py:373` | `unit_lookup.py:262-291`, `herdr.py:1216` (reads `/proc/<pid>/environ`), `stall_watch.py:110`, `herdr_sessions.py:165`, `units_table.py` | **breaks**: unit shows `gone`. A running process's env cannot change: only a relaunch fixes it |
| 16 | Director's own context ("work only in your worktree X, branch Y") | wt, br | `add_unit.py:657-683` | the director | breaks until it is told |
| 17 | Claude session name: `-n` and `--remote-control` at launch, `~/.claude/sessions/<pid>.json` `name` | sess | `add_unit.py:735` (stem); `/rename` | ListAgents, `send.py` via `sessions.addressed` (also `formerNames`, `sessions.py:233-244`), `build_hold.py:420` | label; already looked up |
| 18 | herdr session name, tmux session name | sess | `herdr.generated_session_name` at launch (`herdr.py:731`); `tmux_names.py` follows Claude names | people | label; lookups go by marks |
| 19 | Claude transcript folder `~/.claude*/projects/<encoded wt>/` | wt | Claude | `--resume` (needs the cwd to match) | breaks resume; `rename.py:656` moves it |
| 20 | Trust entry `projects[<wt>]` in each account's `.claude.json` | wt | Claude | Claude at launch | trust prompt; `rename.py:635` copies it |
| 21 | Shutdown restart record (`~/.local/state/shutdown/(masked)/…`): `host.unit`, `session_name`, plan path, cwd | id, sess, wt | `shutdown/session_types.py:21-28` | `agents restart` → `add_unit --resume` | breaks a pending restore |
| 22 | Message queue for an offline name `message/queue/<name>.jsonl` | sess | `send.py:463-480` | `send.py:649` | harmless: keyed by session id when live |

### State outside git

| # | Place | Form | Writer | Reader | Today |
| --- | --- | --- | --- | --- | --- |
| 23-28 | Showrunner scratch: `dailies_input_state/eta_seen.json`, `unit_status/decisions_seen`, `unit_status/blocks_open`, `showrunner_state.json`, `dailies_judgment.json`, `dailies_state.json` | id | dailies, `unit_status.sh`, showrunner | the same | stale; **`rename_state.rename_scratch` already moves all six** (`rename_state.py:155-181`) |
| 29 | `showrunner/<slug>/dailies_last.json` rows | short label | `dailies_input.py:34` | the next dailies' "what changed" | self-heals after one report |
| 30 | Stall-watch stretch `stall-watch/sha256(slug\0id).json` | id | `stall_watch.py:207` | `stall_watch.py` | stall timer resets |
| 31 | Review memory `review-pool/<slug>/memory/<id>.md` | id | `/review:direct` | `review_pool.py:331-340` | lost: the reviewer forgets the unit |
| 32 | Run records `plan-delegate/runs/*.jsonl`, `working_dir` of `run_started` | wt | `/unit:direct` | `unit_lookup.py:112-136`, `units_table.py`, ETA and phase tables | an **open** run is lost (unit reads standing-by); closed runs are history |
| 33 | `buildlog/launches_state.json` cursor keyed by transcript path | wt | `buildlog/launches.py` | the same | one harmless re-read (launch files are named by session and time) |
| 34 | Usage attribution by folder | wt | derived from the Units Worktree cell (`usage_report.py:332-340`) | `/usage` | older folders fall back to the folder name: history |
| 35 | Hanadocs proposal issues, frontmatter `unit:` | id | `proposal_issue.py:138` | `proposals_table.py:116` ("being built by …") | stale label on active issues; closed ones are history |
| 36 | Review ledger `~/.claude/data/review_regime.jsonl` `unit` | id | `merge_checkpoint.py:835` | `review_regime.py report` | history |
| 37 | Outstanding items `showrunner/outstanding/<slug>.json` | prose | showrunner | dailies | stale prose, rare |
| 38 | Logs: `message/log.jsonl`, `message/relay/*`, `notify/*.jsonl`, `accounts/moves.jsonl`, `showrunner/<slug>/log-backups/`, `outstanding/*.history.jsonl`, `nightly-review/*`, `model-study/*`, `claude-model-study/*`, `plan-delegate/review_turns/*`, feature log `--unit` | any | many | reports | history |
| 39 | Hard-coded ids: `NIGHTLY_REVIEW_UNIT` (`add_unit.py:71`, `nightly_review.py:223`), `"review-unit"` (`add_unit.py:243`) | id | code | launch profile, review pool | breaks the special launch: refuse renaming these |
| 40 | Docs and memories naming units by hand (production doc prose, `~/.claude/projects/*/memory/*.md`) | any | people | people | history and provenance |

**Total: 40 places** (scratch rows 23-28 count as six).

## Reducing the places

Rule: store only what cannot be looked up, and never rewrite history.

| Group | Places | Verdict |
| --- | --- | --- |
| Already looked up, nothing to do | 17, 18, 22, 29, 33, 34 (6) | Session names, herdr and tmux names, queues, usage and the dailies diff already follow the source or heal by themselves |
| History, left as written | 6, 8, 9, 36, 37, 38, 40 (7) | Same rule as `/showrunner:rename`: subjects, logs and transcripts keep the old name |
| **Becomes derived** | 7 (1) | The only history that is *read for decisions*. `merge_branch_history` follows the rename commit `chore(<slug>): rename unit <old> to <new>` on the merge branch: entries older than it under `<old>` count as `<new>`. The commit is the record; no alias list is stored |
| Refused | 39 (1) | `review-unit` and `nightly-review-unit` are code-known ids |
| Rewritten by the script | 1-5, 10-13, 15-16, 19-21, 23-28, 30-32, 35 (25) | One registry in the script lists them; apply walks it, verify proves it |
| Unaffected | 14 (1) | |

Physical edits left for a person: **none**. The user's only action is the go.

### Options weighed and rejected

- **An opaque, permanent unit id with a display name looked up.** It would make a
  rename one cell, but every log line, merge subject, message and table would show
  the opaque id or need a lookup, about 20 scripts change, and the branch and worktree
  still carry the readable name. Renames are rare; the readable id stays the key.
- **Dropping the Branch cell and reading it from the worktree.** True to the rule
  (`git -C <wt> branch --show-current`), but retired rows have no worktree and five
  readers would change. The script writes both cells in the same line; not worth it now.
- **Plans that never bake the worktree path.** Would remove place 5. It changes the
  shared plan format (`plan:to_phased_plan`, `/unit:direct`), so it is a separate
  proposal. The script rewrites live plans instead.

## The command

**Name:** `/showrunner:rename_unit <old> <new>`, run in the showrunner session — the user's
2026-10-07 name, and every unit-managing command (`add_unit`, `promote_unit`, `units`)
lives under `showrunner:`. `/unit:rename` is a one-line stub that forwards to it, as
`/unit:delegate` forwards to `/unit:direct`. A one-line revert turns it the other
way round.

**Script:** `scripts/production/rename_unit.py plan|apply|verify <slug> <old> <new>`.
`<old>` and `<new>` take an id or a stem; both normalise to `<stem>-unit`. The
branch and worktree moves reuse `rename.py`'s unit helpers (`rename.py:561-692`),
moved into a shared `unit_move.py` so both renames use one copy.

### plan (writes nothing)

Prints every registry place with its old and new value, and the refusals below.

### Refuses

- `<new>` already in the Units table, or its branch (local or origin) or worktree exists.
- `<old>` or `<new>` is `review-unit` or `nightly-review-unit`; a name with characters
  `add_unit` refuses (`add_unit.py:303`).
- The unit is retired (no worktree; its row is history).
- A phase is in progress: the open run has a phase started and not ended, or a seat
  is live. A rename **between phases of an open run is allowed** and carries the run
  with it (place 32). Calling this myself: a long-lived unit is almost never between
  runs, so "between runs only" would make the command near useless. Risk: one more
  record format to rewrite.
- A form is showing in the unit's pane (`shutdown/settle.py:519`).
- An unmerged checkpoint commit on the unit branch (a merge would write the old id).
- A production rename of this slug is unfinished (`rename.json` not done), or another
  unit rename holds the lock.
- The unit runs on the Mac (version one renames natedev units only).

### apply (journaled; rerun resumes)

1. **Stop** the unit through the shutdown path: `agents down --only <session>`
   (pause and settle as today). A live process cannot change its env mark, and the
   director's context holds the old paths, so a live rename is always a relaunch,
   never an in-place change.
2. **Git**: create and push `<slug>-<new stem>` at the old tip; copy
   `cargoBerthTarget`; `git worktree move` and `repair`; switch.
3. **Claude**: move transcript folders and copy trust entries in every `~/.claude*`
   config dir (existing helpers).
4. **Doc commit** on the merge branch, one commit, subject
   `chore(<slug>): rename unit <old> to <new>`: Unit, Worktree, Branch cells; Hub and
   open Gates cells (whole-token match, not substring); old worktree path and branch
   in the unit's live plan files (Plan cell paths that are not as-built or retired).
5. **State**: `rename_state.rename_scratch(old, new, scratch)`; move the stall-watch
   stretch and the review memory file; rewrite `working_dir` in the open run's records;
   move the `launches_state.json` keys; set `unit:` on active proposal issues.
6. **Restart record**: rewrite the unit's one shutdown record (unit id, cwd, plan
   path, session name). More than one record, or a restore already running: refuse.
7. **Relaunch**: `agents restart --only <unit>`. The restart note says "You are now
   `<new>`; worktree `<path>`, branch `<branch>`; your plan is at `<path>`." The
   session is named after the new stem (see question 1).
8. **Retire** the old branch locally and on origin once the new tip is verified.
9. **Verify** (below). A failing verify leaves the journal open and names the place.

### verify: proof of completeness

- **Live places must be clean.** Each registry place is read for the old id
  (whole token), old branch and old worktree path. Any hit fails.
- **Control.** Each place apply wrote must now contain the new value (a non-empty
  read), and the merge-branch subjects must still contain the old id: if the scanner
  finds nothing in history it is broken, not done.
- **Unknown places fail.** A broad `rg -l` of the three old strings over
  `~/.local/state`, `~/.claude/data`, every `~/.claude*/` config dir, the showrunner
  checkout and the unit's new worktree. Every hit must belong to a registry place
  marked history; an unclassified hit fails and names the file. Paths under
  `~/.local/state/shutdown/` print as `(masked)`. This is what catches a place added
  later: it fails until someone files it in the registry.
- **Live proof.** `unit_lookup.py pane <slug> <new>` finds the relaunched session,
  and `/showrunner:units` shows it with its open phase.

## Phases

Each phase is one checkpoint on `build-followups-enh-showrunner`, gated with
`merge_gate.py --tests-only` and merged by the showrunner. The work stays in
`/home/natepiano/worktrees/claude-build-followups-enh-showrunner`.

### Phase 1 — merge history follows rename commits (S)

- `merge_checkpoint.merge_branch_history` reads the first-parent subjects newest first. A subject
  `chore(<slug>): rename unit <old> to <new>` adds the rename; every older merge entry under
  `<old>` then counts as `<new>`. A chain (`a` to `b`, later `b` to `c`) resolves to `c`, and a
  later unit that reuses `<old>` keeps its own newer entries. The commit is the record; no alias
  list is stored.
- `LAST_MERGED`, the first-merge gate and `has_code_merge` read through it unchanged.
- `commands/showrunner/produce.md` (`LAST_MERGED`) says a rename commit carries the history over.
- Tests: `LAST_MERGED`, the first-merge gate and `has_code_merge` across a rename; a chained
  rename; a reused old name after a rename.

### Phase 2 — shared unit moves, `rename_unit.py plan` and `apply` (L)

- `scripts/production/unit_move.py` takes `rename.py`'s unit helpers (branch create and push,
  worktree move and repair, transcript folders, trust entries) so both renames use one copy, and
  adds the three missing moves: `branch.<br>.cargoBerthTarget`, the open run's `run_started`
  `working_dir`, and the `buildlog/launches_state.json` cursor keys. `rename.py unit` uses it,
  which closes its own `cargoBerthTarget` and open-run gaps.
- `scripts/production/rename_unit.py plan|apply <slug> <old> <new>`: `<old>` and `<new>` take an
  id or a stem and normalise to `<stem>-unit`. `plan` writes nothing and prints every registry
  place with its old and new value and every refusal. `apply` is journaled under a lock and
  resumes on rerun, steps 1-8 of **apply** above; the doc commit uses whole-token matching.
- Tests run on fixture repos and a fake state root: every refusal, every step, resume after each
  step, and `rename.py unit` still passing its own suite on the shared module.

### Phase 3 — verify, the shutdown record, the commands and the as-built (M)

- `rename_unit.py verify`: the place registry (live places clean, apply's places hold the new
  value, merge history still holds the old id as control) and the broad sweep over
  `~/.local/state`, `~/.claude/data`, every `~/.claude*/` config dir, the showrunner checkout and
  the new worktree, where an unclassified hit fails and names its file, shutdown paths masked.
- The shutdown restart record rewrite (step 6), with its refusals.
- `/showrunner:rename_unit` (`commands/showrunner/rename_unit.md`) and the `/unit:rename` stub
  that forwards to it; a paragraph in `docs/production_format.md` (Units); the as-built
  (`docs/as-built/unit-rename.md`), and this plan removed.

### Phase 4 — live check and first real use (S)

- A live check on a fake production (`fake_showrunner.py`): plan, apply, verify, and a rerun after
  a stop between steps.
- First real use, run by the showrunner with this unit watching: `test-threads-unit-unit` to
  `test-threads-unit`, then `feature-log-unit-unit` to `feature-log-unit` (decision 2).

## Risks

- A rename between phases of an open run rewrites one more record format (the run records); a
  rename during a phase is refused.
- The broad sweep fails on any place added later until it is filed in the registry; that is the
  intent, and its message names the file.
- Version one renames natedev units only; a Mac unit is refused.
