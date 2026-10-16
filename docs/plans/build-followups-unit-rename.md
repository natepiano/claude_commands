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
| 14 | Unit plan header `> **Production:**` | id (the header names the unit: `phase_table.py:1236` `PRODUCTION_PATTERN`, `to_production.md:84`) | to_production | `/unit:direct`, `phase_table.py` | breaks note ownership; **corrected by the team review** (was "production only") |

### Sessions

| # | Place | Form | Writer | Reader | Today |
| --- | --- | --- | --- | --- | --- |
| 15 | Process env marks `SHOWRUNNER_UNIT` (slug) and `SHOWRUNNER_UNIT_ID` | id | `add_unit.py:711-712`, `unit_lookup.py:373` | `unit_lookup.py:262-291`, `herdr.py:1216` (reads `/proc/<pid>/environ`), `stall_watch.py:110`, `herdr_sessions.py:165`, `units_table.py` | **breaks**: unit shows `gone`. A running process's env cannot change: only a relaunch fixes it |
| 16 | Director's own context ("work only in your worktree X, branch Y") | wt, br | `add_unit.py:657-683` | the director | breaks until it is told |
| 17 | Claude session name: `-n` and `--remote-control` at launch, `~/.claude/sessions/<pid>.json` `name` | sess | `add_unit.py:735` (stem); `/rename` | ListAgents, `send.py` via `sessions.addressed` (also `formerNames`, `sessions.py:233-244`), `build_hold.py:420` | label; already looked up |
| 18 | herdr session name, tmux session name | sess | `herdr.generated_session_name` at launch (`herdr.py:731`); `tmux_names.py` follows Claude names | people | label; lookups go by marks |
| 19 | Claude transcript folder `~/.claude*/projects/<encoded wt>/` | wt | Claude | `--resume` (needs the cwd to match) | breaks resume; `rename.py:656` moves it |
| 20 | Trust entry `projects[<wt>]` in each account's `.claude.json` | wt | Claude | Claude at launch | trust prompt; `rename.py:635` copies it |
| 21a | Fix-pipeline checkout redirect `fix.conf [active_checkout]` (`retarget_fix.py:10`), path relative to `~/rust`, member suffixes | wt | `retarget_fix.py apply` | `fix-usage.sh:131`, `agent_assignments.sh:21` | fixes target a removed checkout; **added by the team review** |
| 21 | Shutdown restart record (`~/.local/state/shutdown/(masked)/…`): `host.unit`, `session_name`, plan path, cwd | id, sess, wt | `shutdown/session_types.py:21-28` | `agents restart` → `add_unit --resume` | breaks a pending restore |
| 22 | Message queue for an offline name `message/queue/<name>.jsonl` | sess | `send.py:463-480` | `send.py:649` | harmless: keyed by session id when live |

### State outside git

| # | Place | Form | Writer | Reader | Today |
| --- | --- | --- | --- | --- | --- |
| 23-28 | Showrunner scratch: `dailies_input_state/eta_seen.json`, `unit_status/decisions_seen`, `unit_status/blocks_open`, `showrunner_state.json`, `dailies_judgment.json`, `dailies_state.json` | id | dailies, `unit_status.sh`, showrunner | the same | stale; **`rename_state.rename_scratch` already moves all six** (`rename_state.py:155-181`) |
| 29 | `showrunner/<slug>/dailies_last.json` rows | short label | `dailies_input.py:34` | the next dailies' "what changed" | self-heals after one report |
| 30 | Stall-watch stretch `stall-watch/sha256(slug\0id).json` | id | `stall_watch.py:207` | `stall_watch.py` | stall timer resets |
| 31 | Review memory `review-pool/<slug>/memory/<id>.md` | id | `/review:direct` | `review_pool.py:331-340` | lost: the reviewer forgets the unit |
| 31a | Reviewer leases `review-pool/<slug>/pool.json` `reviewers.*.lease.unit` | id | `review_pool.py:166` | `review_pool.py:412` `_unit_reviewers` | the unit gets a new reviewer; a later unit reusing the old id inherits the old context; **added by the team review** |
| 32 | Run records `plan-delegate/runs/*.jsonl`: `working_dir`, `worktree`, `branch`, absolute `plan_doc` / `project_plan_doc` | wt, br | `/unit:direct` | `unit_lookup.py:112-136`, `units_table.py`, ETA and phase tables (`phase_table.py:952`) | an **open** run is lost (unit reads standing-by); closed runs are history |
| 32a | Recorder state `<session_dir>/progress_history_state.json`: the same fields; `_event` stamps every new event from it (`progress_history.py:1183`) | wt, br | `progress_history.py start-run` | the recorder | the next event writes the old names back; **added by the team review** |
| 33 | `buildlog/launches_state.json` cursor keyed by transcript path | wt | `buildlog/launches.py` | the same | one harmless re-read (launch files are named by session and time) |
| 34 | Usage attribution by folder | wt | derived from the Units Worktree cell (`usage_report.py:332-340`) | `/usage` | older folders fall back to the folder name: history |
| 35 | Hanadocs proposal issues, frontmatter `unit:` (scoped by its `showrunner` field: the folder is shared across productions) | id | `proposal_issue.py:138` | `proposals_table.py:116` ("being built by …") | stale label on active issues; closed ones are history |
| 35a | Hanadocs phase notes, frontmatter `unit:` | id | `phase_table.py refresh` | `phase_table.py:1294` `_note_ownership`, `prune` (`:1577`) | refresh duplicates the note or prune archives the live one; **added by the team review** |
| 35b | Rename journals `rename_unit.json`, `rename.json` | id, br, wt | the renames | the renames | recovery data: kept, classified for verify |
| 36 | Review ledger `~/.claude/data/review_regime.jsonl` `unit` | id | `merge_checkpoint.py:835` | `review_regime.py report` | history |
| 37 | Outstanding items `showrunner/outstanding/<slug>.json` | prose | showrunner | dailies | stale prose, rare |
| 38 | Logs: `message/log.jsonl`, `message/relay/*`, `notify/*.jsonl`, `accounts/moves.jsonl`, `showrunner/<slug>/log-backups/`, `outstanding/*.history.jsonl`, `nightly-review/*`, `model-study/*`, `claude-model-study/*`, `plan-delegate/review_turns/*`, feature log `--unit` | any | many | reports | history |
| 39 | Hard-coded ids: `NIGHTLY_REVIEW_UNIT` (`add_unit.py:71`, `nightly_review.py:223`), `"review-unit"` (`add_unit.py:243`) | id | code | launch profile, review pool | breaks the special launch: refuse renaming these |
| 40 | Docs and memories naming units by hand (production doc prose, `~/.claude/projects/*/memory/*.md`) | any | people | people | history and provenance |

**Total: 46 places** (scratch rows 23-28 count as six; 21a, 31a, 32a, 35a, 35b were added by the team review).

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

## Team review, 2026-10-16 (the user's goal: the smallest rename surface)

The user asked for a team review of this plan before building on, with one goal:
remove every redundant place a name is written, so a rename touches the smallest
surface, with automation and config files driving the names. Four reviewers (names
and derivation; registry completeness; risk and resume; simplicity) read the plan and
the Phase 2 code. Their findings, and what this plan now does about each:

### Names now derived instead of stored

| Was stored | Now | Places dropped from the rename |
| --- | --- | --- |
| Three naming rules: `add_unit.launch_request`, `rename_unit._build`, `rename._prepare_unit`. They disagree: the real `mul_add-unit` (branch `build-followups-mul-add`) cannot be renamed, and a retried `foo_bar` refuses its own journal | One naming module, `unit_names.py`: id normalisation (strip one `-unit`, add `-unit`, the character rule), the filesystem stem (`_` to `-`), and the production's branch and worktree defaults. Creation and both renames call it; the Branch and Worktree cells are written by it, never hand-built | The cells stay (adopted units and retired rows keep their own values), but no caller derives them on its own |
| Worktree path and branch baked into plan Work Orders (place 5) | `/unit:direct` composes each Work Order with the worktree and branch it reads from git at dispatch (`git rev-parse --show-toplevel`, `git branch --show-current`); a path in plan text is never the source. `plan:to_phased_plan` stops writing the absolute worktree path, and `add_unit.prompt_for` names the rule | 5: rename_unit no longer edits plan prose. That also removes the whole-token plan edit that rewrote `docs/plans/demo-alpha.md` to `demo-beta.md` |
| Shutdown restart record: cwd, plan path, session name (place 21) | `restart._unit_argv` resolves the worktree, plan and session name from the Units row by unit id (it already reads the Plan cell); the record keeps them only as the recovery snapshot | 21 shrinks to one field, `host.unit` |
| `cargoBerthTarget` copied verbatim | The value is the production's merge branch unless set otherwise: a unit rename keeps it; a production rename maps a target equal to the old merge branch to the new one and keeps any other | none, but the production rename no longer leaves a target on a deleted branch |
| `launches_state.json` cursor keys (place 33) | Not moved: one harmless re-read. Apply step 5 no longer lists it (the plan said both) | 33 |

**Considered and not taken: a permanent internal unit key.** Reviewer 1 proposed an
immutable key for the stores keyed by unit id (23-28, 30, 31, 31a, 32, 35), with the
readable id kept for display. My call: no. The costly half of a rename is the part
that must carry the readable name: the branch, the worktree, Claude's transcript
folders and trust entries, process env marks and the relaunch. A key removes only the
cheap half (atomic moves of files the script already owns, each journaled), and adds a
second identity that every reader resolves and every person debugging the files has
to look up. Risk: if renames become frequent the cheap half grows; the registry makes
adding the key later a contained change.

### Correctness and resume (recorded; built in Phases 2 and 3)

- **Journal first.** `apply` saves the validated request as step 0 before any
  mutation; a resumed run checks the request against the journal (normalised the same
  way) before reading progress, and `plan` refuses an unfinished journal for a
  different request, naming its resume command. Fresh preflight and recovery are
  separate: recovery accepts the destinations this journal created.
- **Revalidate at each step.** Before each step: the checkout is on the merge branch
  and clean, the unit is still stopped, no worker or process has its cwd inside the
  worktree (`/proc/*/cwd`, plus the run's worker inventory), and the tips match.
- **One production lock.** `rename.py` and `rename_unit.py` take the same lock, kept
  outside every directory a rename moves (`~/.local/state/showrunner/rename.lock`),
  and each refuses while the other's journal is unfinished.
- **A rename claim.** While a `rename_unit.json` names a unit, `add_unit.py` and
  `agents restart` refuse to launch it under either name except through the rename.
- **Doc commit by hash.** The journal records the rename commit's hash before the push;
  recovery checks it is on the merge branch and retries only the push.
- **The rename reaches the unit.** After the doc commit, apply merges it into the new
  unit branch in the moved worktree while the unit is stopped (abort and refuse on a
  conflict), pushes that tip, and verify reads it.
- **Retire by lease.** The old branch is deleted on origin with
  `--force-with-lease=<branch>:<tip>` and locally with `git update-ref -d <ref> <tip>`;
  a moved ref leaves the step open and names the tip.
- **Open run and recorder state together.** `progress_history.py` gains one
  recorder-owned operation that moves an open run (record and `progress_history_state.json`,
  absolute plan paths included). Both renames call it; run discovery is shared with
  `unit_lookup`.
- **Newest run, finished too.** `unit_lookup.run_state` reads the newest run for the
  worktree, so the newest finished run moves as well as an open one; otherwise a finished
  unit reads standing-by (production-rename review).
- **Merge before move.** Before the git step, apply merges `origin/<merge branch>` into the
  unit branch in the old worktree while the unit is stopped; a conflict aborts and leaves
  the unit under its old name. The journal keeps the original and merged tips apart.
- **Reviewer leases** move with the memory file under `pool.lock`, thread ids and
  ordinals kept.
- **Proposal issues** are changed only when their `showrunner` field names this
  production.
- **Phase notes** (35a): the rename rewrites `unit:` in the production's notes through
  `phase_table.py`'s own reader, then refreshes.
- **Hub and Gates** edits touch only the Owner and Other cells and the Waiting and
  Waits-on cells, never a File cell.
- **Plan headers** (14): the `> **Production:**` header's unit id in the unit's live plans
  is rewritten, and other units' live plans keep their prose (history).
- **fix.conf redirects** (21a) under the old worktree are rewritten, member suffixes kept.
- **Mac.** A unit whose session the cross-machine inventory places on the Mac, or whose
  machine cannot be established, is refused.
- **Trust copy.** `.claude.json` is rewritten by running sessions, so the
  read-modify-write stays one tight read, write and read-back (as `rename.py` does
  today); the risk of losing another session's write in that window is accepted.
- **Verify by field.** Each registry place states its postcondition: the new value
  required, the old value allowed (the retained `projects[<old wt>]` trust entry), or
  recovery data (the journals). A test injects one stale live reference and expects a
  failure.

### Simplicity (recorded)

- The registry is data in `rename_unit.py`: `plan` prints from it with concrete old and
  new values and runs every read-only refusal check; `apply` and `verify` walk it.
- `rename.py` calls `unit_move` directly; the `_git`, `_exists`, `_move`, `_plan_paths`
  aliases and the three deletion wrappers go.
- `rename_state` exposes a small public persistence API (`read_json`, `write_json`,
  `write_lines` over one atomic writer); no `reportPrivateUsage` suppressions.

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
   `chore(<slug>): rename unit <old> to <new>`: Unit, Worktree, Branch cells; Hub Owner
   and Other cells and open Gates Waiting and Waits-on cells (whole-token match); the
   `> **Production:**` header in the unit's live plans. Plan prose is not edited (Work
   Orders read the worktree from git). Then merge that commit into the new unit branch
   in the moved worktree and push it.
5. **State**: `rename_state.rename_scratch(old, new, scratch)`; move the stall-watch
   stretch, the review memory file and the reviewer leases; move the open run (record
   and recorder state); set `unit:` on this production's active proposal issues and
   phase notes; rewrite `fix.conf` redirects under the old worktree.
6. **Restart record**: rewrite `host.unit` in the unit's one shutdown record; restart
   resolves the worktree, plan and session name from the Units row. More than one
   record, or a restore already running: refuse.
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

- `scripts/production/unit_names.py`: the one naming rule (team review). `add_unit.py`,
  `rename.py` and `rename_unit.py` call it.
- `scripts/production/unit_move.py` takes `rename.py`'s unit helpers (branch create and push,
  worktree move and repair, transcript folders, trust entries) so both renames use one copy, and
  adds the missing moves: `branch.<br>.cargoBerthTarget` (kept, or mapped to the new merge
  branch by a production rename), and the open run through `progress_history.py`'s own move
  (record and recorder state). `rename.py unit` uses it, which closes its own
  `cargoBerthTarget` and open-run gaps. `rename.py` calls it directly (no aliases).
- The team review's resume and safety items: journal at step 0, request check, revalidation
  at each step, the shared production lock, the rename claim honoured by `add_unit.py`, the
  doc commit by hash, the merge into the unit branch, retire by lease, leases, proposal and
  phase-note scoping, Hub and Gates cell limits, `fix.conf` redirects, the Mac refusal.
- Work Orders read the worktree and branch from git at dispatch
  (`docs/delegate/compose_work_order.md`, `add_unit.prompt_for`, `plan:to_phased_plan`).
- `scripts/production/rename_unit.py plan|apply <slug> <old> <new>`: `<old>` and `<new>` take an
  id or a stem and normalise to `<stem>-unit`. `plan` writes nothing and prints every registry
  place with its old and new value and every refusal. `apply` is journaled under a lock and
  resumes on rerun, steps 1-8 of **apply** above; the doc commit uses whole-token matching.
- Tests run on fixture repos and a fake state root: every refusal, every step, resume after each
  step, and `rename.py unit` still passing its own suite on the shared module.

**As built (Phase 2).** `apply` runs seven journaled steps: merge `origin/<merge branch>` into
the unit, git, Claude, the doc commit, the rename commit merged into the unit branch, state, and
retiring the old branch. Phase 3 adds stop, the restart record, relaunch and verify around them, so
until then `apply` needs a stopped unit. Calls made while building:
- The Mac refusal is the linked-worktree check: a Mac unit's worktree is not on this machine, so
  the rename makes no cross-machine call.
- Phase notes get their `unit:` key only; the unit's next refresh rewrites the rest of the note.
- The rename claim is checked in `add_unit.preflight`, which `agents restart` launches through, so
  one check covers both.
- The launches cursor is not moved (one harmless re-read).

### Phase 3 — verify, the shutdown record, the commands and the as-built (M)

- `rename_unit.py verify`: the place registry (live places clean, apply's places hold the new
  value, merge history still holds the old id as control) and the broad sweep over
  `~/.local/state`, `~/.claude/data`, every `~/.claude*/` config dir, the showrunner checkout and
  the new worktree, where an unclassified hit fails and names its file, shutdown paths masked.
- The shutdown restart record (step 6): `restart._unit_argv` resolves worktree, plan and
  session name from the Units row; the rename rewrites `host.unit` only, with its refusals.
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
