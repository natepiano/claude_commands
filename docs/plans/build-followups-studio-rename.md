# Rename the build-followups production to studio

> Proposed 2026-10-16 14:45 EDT by a natedev subagent, for the user ("studio is good - task an agent to plan a full on rename of all of the current builder-followups production").
> Status: approved by the user 2026-10-16; team-reviewed 2026-10-16 (section 5). Builder: enh-showrunner-unit, after the unit rename. Survey taken 2026-10-16 14:35 EDT.
> **Production:** build-followups (enh-showrunner-unit)

## Situation

A production-rename tool already exists: `/showrunner:rename` with
`scripts/production/rename.py` (`plan` / `apply` / `unit` / `finish`), built by
build-report-unit (as-built `docs/as-built/production-rename.md`). It has never
been run on a real production. Run on this one today, it would fail or leave
gaps:

| # | Gap | Effect today |
| --- | --- | --- |
| G1 | `apply` preflight needs `git status --porcelain` empty; the untracked logs `docs/plans/build-followups-log.md` and `-log-recovered.md` sit in the showrunner checkout | **apply refuses** |
| G2 | The notifier conf keeps `PROMPT_FILE=…/showrunner/build-followups/prompt.txt` and `FROM=showrunner-timer-build-followups`; `prompt.txt` itself names the old doc and checkout paths | the timer's prompt points at a moved file |
| G3 | Doc title, `Log:` and `Showrunner checkout:` lines are not rewritten; the logs are not moved | stale live fields |
| G4 | Plan files keep the `build-followups-` prefix (16 in `docs/plans/`, 3 in `docs/as-built/`); links to them are not rewritten | the old name stays in every live plan path |
| G5 | `config/dailies.conf` keys `build-followups.nightly-queue` and `.proposals` | the studio dailies lose both lines |
| G6 | `outstanding/<slug>.history.jsonl` stays under the old slug | history split in two files |
| G7 | `unit` checks only for a live Claude; Codex seats, cargo and shells with their cwd in the worktree are not checked (3 Codex seats today) | a seat loses its cwd mid-work |
| G8 | `unit` leaves the open `/unit:direct` run record (`working_dir`, `branch`, `plan_doc`, `project_plan_doc`) and the shutdown restart record on old paths | the unit reads standing-by and loses its ETA tables; a restart goes to the old cwd |
| G9 | `unit` does not stop or relaunch; the showrunner does both by hand | 18 hand stop/relaunch cycles |
| G10 | `finish` refuses while a non-retired row has an old-prefixed branch; followups-unit, notifier-unit and ratio-unit have no worktree, so `unit` cannot move them | **finish refuses** |
| G11 | The showrunner checkout `claude-build-followups-trunk` is never moved; merged orphan branches and worktrees are never cleaned | the old name stays on 16 refs and 10 paths |
| G12 | No `verify`: nothing proves completeness | — |

G8 is half-covered by the accepted unit-rename plan (its phase 2 moves `working_dir`
into a shared `unit_move.py`). The rest is new.

## 1. Every place the name lives

R = rewrite, H = leave as history, D = derived automatically (follows the source or regenerates).

### Git refs and worktrees (`~/.claude`, natedev only)

| Place | Count | Verdict | By |
| --- | --- | --- | --- |
| Merge branch `build-followups` (local, origin) | 1 | R: `studio` at the same tip; old deleted | apply, finish |
| Unit branches with a live Units row and worktree | 18 | R: `studio-<stem>`; old deleted | unit |
| Unit worktrees `~/worktrees/claude-build-followups-<stem>` | 18 | R: `claude-studio-<stem>` | unit |
| Showrunner checkout `claude-build-followups-trunk` | 1 | R: `claude-studio-trunk` | finish (G11) |
| Merged branches with no worktree: berth-suffix, heredoc-guard, shrink-eof, stalls; origin-only notifier, ratio, unit | 7 | R: delete (ancestor-checked) | finish (G11) |
| Merged branches with a worktree outside the Units table: review-pool (retired row), statusline-label, unit-effort, 4 Agent-tool worktrees under `~/.claude/.claude/worktrees/`, new-account, statusline-role | 9 | R: remove worktree and branch when clean, merged and no process uses it | finish (G11) |
| `branch.<b>.remote/.merge` entries | 32 branches | D: go with the branch | git |
| `branch.<b>.cargoBerthTarget` | 0 (`~/.claude` has no `berth.toml`) | — | — |
| Merge and checkpoint commit subjects (392 `Merge … phase` subjects) | all | H. LAST_MERGED still works: `studio` starts at the old tip and the parser reads unit ids, not branch names | — |

Totals: 32 local and 34 origin branches, 28 worktrees.

### Tracked files on the merge branch (38 files mention it, 19 are named for it)

| Place | Files / mentions | Verdict |
| --- | --- | --- |
| Production doc: title, Merge branch, Log, Showrunner checkout | 1 / 4 live fields | R (apply; G3) |
| Production doc: Units Worktree and Branch cells | 18 rows | R (unit) |
| Production doc: Source plans line, Status history, retired rows' cells | rest of 29 | H (a Formerly line is added) |
| `docs/plans/build-followups-*` (15 plans and diffs besides the production doc) | 15 named, 8 with content mentions | R: `git mv` to `studio-*`, Production headers and paths inside rewritten (apply; G4) |
| `docs/as-built/build-followups*.md` | 3 named | R: renamed for their content (`build-followups.md` is not "studio") by hand in the same commit, my call |
| As-built links to renamed plan paths | 4 files | R (G4) |
| As-built provenance prose ("built in build-followups") | 8 files | H |
| `config/dailies.conf` keys, `config/README.md` | 2 / 3 | R (G5) |
| Test fixtures (12 files under `scripts/`) | 12 / 123 | R: `studio` in fixtures, no behaviour change |
| `scripts/notify/push_catalog.py` long-name examples and their quote in `push_texts_reviewed.json` | 2 / 7 | R: a neutral long name, both files together |
| `scripts/model_study/roster.json` | 1 / 7 | H: record of a finished study |
| `commands/`, `CLAUDE.md`, `settings.json`, `docs/production_format.md` | 0 | — |
| Unit branches | — | D: units merge the merge branch at every phase start; the one unit-only plan (`build-followups-unit-rename.md`) is merged before the rename runs |

### State on natedev (`~/.local/state`)

| Place | Count | Verdict |
| --- | --- | --- |
| `notifier/showrunner-build-followups/` (the update-timer instance; one `session-notifier.timer` runs every instance, no systemd unit carries the slug) | 1 | R: moved by apply; `PROMPT_FILE`, `FROM`, `CHECK` rewritten (G2) |
| `showrunner/build-followups/` (prompt.txt, dailies_last.json, log-backups, 3 old unit prompts) | 1 dir | R: moved by apply; prompt.txt paths rewritten (G2); log-backups content H |
| `showrunner/outstanding/build-followups.json`, `footers-off/build-followups` | 2 | R: moved by apply |
| `showrunner/outstanding/build-followups.history.jsonl` | 1 | R: moved (G6) |
| `review-pool/build-followups/` | 1 | R: moved by apply |
| Stall-watch files `sha256(slug\0unit).json` | 23 rows | R: moved by apply |
| `review-health-<slug>`, `bevy-upgrade-<slug>` notifier instances | 0 | — |
| Open `/unit:direct` run records (herdr-trial, screenshot, two-accounts, mul_add today) | 4 | R: unit (G8) |
| Closed run records, `review_turns/`, `delegate-archive/` | 55 + 343 + 22 | H |
| Pending scheduled prompt naming model-study's worktree (`conversation-pause/scheduled-prompts/`) | 1 | R: unit (G8) |
| Shutdown records naming it | 55, all under `history/` | H; no pending restore names it |
| `message/queue/build-followups.jsonl`, `message/relay/build-followups.jsonl` (2 messages to a session name nobody holds, 2026-10-08) | 2 | H |
| Nightly review, usage-model, buildlog, notify, mul-add-hook, screenshot-analysis, plan-backlog ledger and proposals | ~300 | H |

### Sessions and account data

| Place | Count | Verdict |
| --- | --- | --- |
| Claude processes with `SHOWRUNNER_UNIT=build-followups` | 16 | D: Formerly resolves the old slug; each relaunch writes `studio` |
| Codex seats with cwd in a worktree (two-accounts, mul_add, herdr-trial) | 3 | stopped with their owner by `agents down`; `unit` refuses while any process cwd is inside (G7) |
| Claude project folders named by worktree path, all `~/.claude*/` dirs | 200: 105 named for a worktree, 95 for a scratchpad | R: live ones moved by unit; scratchpad folders H |
| Trust entries `projects[<worktree>]` in each `.claude.json` | per worktree | R: copied by unit |
| Session names, herdr and tmux names | 0 contain the slug | — |
| Showrunner session | natedev, unchanged | — |

### Other repos and notes

| Place | Count | Verdict |
| --- | --- | --- |
| `/etc/nixos` comments | 5 lines, 5 files | R 3 (two herdr.nix plan paths, nightly-review.nix "build-followups' permanent unit"); H 2 (agents.nix and cargo-config.toml provenance). Committed by natedev |
| Memories (16 distinct files, shared across 5 config dirs) | 16 | R 2 live pointers (postmaster-trial-window names a plan path; screenshot-unit-owns-hana-shot names the branch); H 14 provenance |
| Hanadocs phase notes `production_file` GitHub link | 16 | D: `phase_table.py refresh` after promotion |
| Hanadocs `agents/*.md`, `agent sessions.md` (worktree paths) | 3 | D: next agent-notes refresh after the moves |
| Hanadocs change-review list rows, `archive/natedev/build-followups/` | 2 | H |
| hana `docs/hana/tool-based-ui-production.md` (retired row, 13 checkouts) | 1 line | H |

### Mac (`ssh mac`, read only)

| Place | Count | Verdict |
| --- | --- | --- |
| `~/.claude` | on main; 0 branches, 0 worktrees | D: promotion fast-forwards main |
| `~/.local/state` | 0 named paths; 2 files (buildlog mirror of natedev, mac-test mirror of a hana doc) | H / D |
| Claude project folders, launchd jobs | 0 | — |
| `~/nixos` | same repo as `/etc/nixos` | D: pull |

## 2. Order of operations

Run by the showrunner (natedev) with the finished command, after the build
below is merged and promoted. **Units are moved one at a time; no unit waits
for another.** Merges keep working throughout: the showrunner merges unit
branches into `studio` whatever the unit branch is called.

| Step | What | Units paused | Verified by |
| --- | --- | --- | --- |
| 0 | Mark followups-unit, notifier-unit and ratio-unit rows retired (runs done, no worktree; G10), my call. Push the unpushed merge-branch commit | none | `rename_production plan` lists no refusal |
| 1 | `plan build-followups studio`; read every line | none | output |
| 2 | `apply`: doc, Formerly, plan files and links, logs, timer, state, stall-watch, review pool, dailies keys, new merge branch `studio` pushed | none | `showrunners.py address build-followups` and `… studio` give the same answer; timer CHECK passes |
| 3 | `unit` for the 2 gone units (cache-evict, delegate-rename) | none (no session) | worktree and branch at new names |
| 4 | `unit` for the 11 idle units: build-report, model-study, phase-tables, shutdown, test-threads, feature-log, bevy-upgrade, message-clarity, herdr-trust, herdr-panes, nightly-review (daytime, outside its nightly job). Each: stop (idle stops at once), move, merge `studio` in, relaunch with a note naming its new worktree, branch and plan | each for about a minute | per unit: `unit_lookup.py pane studio <unit>` finds it; `/showrunner:units` shows the same phase |
| 5 | `unit --wait` for the 4 working units: herdr-trial, two-accounts, screenshot, mul_add. Each pauses at its next safe point, then the same as step 4 | each from its safe point until relaunch | as step 4, plus its open run shows the same "N of M", not standing-by |
| 6 | enh-showrunner-unit last, once its build run has ended | as step 4 | as step 4 |
| 7 | `finish`: deletes old merge branch; moves the showrunner checkout; deletes or removes the 16 merged orphan branches and worktrees (new-account and statusline-role after those natedev subagents finish) | none | `git for-each-ref` and `git worktree list` show no old name |
| 8 | Promote (`~/.claude` and Mac main); `phase_table.py refresh`; nixos comments and the 2 memories | none | — |
| 9 | `verify studio` | none | below |

**verify** reuses the unit-rename sweep: every registry place is read for the
old slug, old branch prefix and old worktree prefix; any hit in a live place
fails. A broad `rg` over `~/.claude` (every ref), all `~/.claude*/` dirs,
`~/.local/state`, `/etc/nixos`, `~/rust/hanadocs`, process environments and
the Mac (`ssh mac`, printing `rc=$?`) must classify every hit as history, or it
fails naming the file; paths under `~/.local/state/shutdown/` print masked.
Control: the merge-branch subjects must still contain `build-followups`, so a
scanner that reads nothing fails instead of passing.

## 3. Build: extend the existing tool, do not add a sibling

**Recommended: no new script and no one-off.** `rename.py` is the production
rename; G1-G12 are defects in it, so they are fixed there. A one-off hand
rename would be a workaround and would leave the tool broken for the next
production. The command is renamed `/showrunner:rename_production` so it sits
beside `/showrunner:rename_unit`; no stub, one-line revert, my call.

| Phase | What | Size |
| --- | --- | --- |
| 1 | apply: untracked `<old>-log*.md` allowed and moved; title, Log and Showrunner checkout lines; `git mv` of `docs/plans/<old>-*` with path rewrites across tracked text files; notifier `PROMPT_FILE`/`FROM` and prompt.txt; history.jsonl; `config/dailies.conf` keys (G1-G6) | M, ~250 lines with tests |
| 2 | unit on top of unit-rename's `unit_move.py`: stop through `agents down --only`, refuse on any process cwd inside the worktree, merge the new merge branch in (abort and refuse on conflict), open run's plan and branch fields, scheduled prompts, shutdown record, relaunch with the new-paths note; `--wait` for working units (G7-G9) | M, ~250 |
| 3 | finish: checkout move; orphan branch and worktree clean-up, each ancestor-, clean- and process-checked; `verify` with the history classes above; command rename and as-built (G10-G12) | M, ~300 |
| 4 | Live check on `fake_showrunner.py` with an untracked log, a non-Claude process in a worktree, an open run and orphan branches; then step 0-9 above on build-followups | S |

**Builder: enh-showrunner-unit**, as a second run right after its unit-rename
run. Phase 2 builds on `unit_move.py` and phase 3 reuses the unit-rename sweep,
both of which that run writes; a second unit editing `rename.py` while it is
being split would conflict. build-report-unit (author of `rename.py`, idle) is
the fallback if enh-showrunner-unit is busy elsewhere.

## 4. Risks

| Risk | Handling |
| --- | --- |
| A working unit's open run is lost when its worktree moves | phase 2 rewrites the run record while the unit is stopped; verify checks the phase reads the same |
| A Codex seat or cargo build loses its cwd | `agents down` stops owned workers; `unit` refuses while any process cwd is inside |
| Two processes on one session corrupt it | relaunch only after the shutdown path confirms the pid gone (existing) |
| A unit merges the frozen `origin/build-followups` between apply and its move | harmless: the tip is frozen, nothing is lost; finish refuses until every live row has moved |
| Plan-file rename conflicts with a unit's edits to its plan | git follows the rename on merge; `unit` merges `studio` while the unit is stopped and refuses on conflict, leaving the unit runnable under its old name |
| The builder is also renamed | enh-showrunner-unit moves last, after its run ends |
| nightly-review's night job finds no unit | moved in daytime; the launcher finds its production by unit, not by slug |
| A running Claude drops the copied trust entry | existing read-back with three retries |
| Locked Agent-tool worktree (owner pid is builder-report's Claude) | removed only after build-report-unit's move, once that pid is gone |
| Deleting merged orphan branches loses work | deleted only when an ancestor of `studio` (checked at delete time); recoverable from the merge commits; unmerged ones refuse |
| History rewritten by accident | subjects, logs, log-backups and transcripts are never edited; no force-push (existing invariant) |
| Hanadocs links to GitHub break between promotion and refresh | `phase_table.py refresh` runs in step 8 |

## 5. Team review, 2026-10-16 (the user's goal: the smallest rename surface)

The user asked for a team review before the build, with one goal: remove every
redundant place a name is written, so a rename touches the smallest surface, with
automation and config files driving the names. Four reviewers (names and derivation;
registry completeness; risk and order; simplicity and shared code) read this plan,
`rename.py` and the unit-rename code. What the plan now does:

### Names now derived instead of stored

| Was stored | Now | Drops from every later production rename |
| --- | --- | --- |
| Plan files named `<slug>-<work>.md`, renamed `build-followups-*` to `studio-*` | Plans are named for their work: this rename moves `docs/plans/build-followups-<work>.md` to `docs/plans/<work>.md` (collision-checked; a clash keeps a short qualifier), and `add_unit.stub_plan` and `/producer:to_production` stop prefixing the slug. The production doc and its log keep `<slug>-production.md` and `<slug>-log.md` (the slug is read from that file name) | 15 plan files, their links in 4 as-built docs, and the plan pointers in run records and prompts |
| Timer fields `PROMPT_FILE`, `FROM`, `CHECK` and the filled `prompt.txt`, rewritten by hand rules | `update_registration.py`'s pure field and prompt generators are split from `register` and called by both registration and the rename, from the current production context; the paused/enabled state and schedule are kept (this production's updates are off) | the rename's own copy of the timer rules |
| Three naming rules for unit branches and worktrees; `add_unit` builds the branch from the slug but the worktree from the checkout's folder name | One naming module (`unit_names.py`, shared with the unit rename): repository prefix, production slug and unit stem kept apart; existing rows use their Units cells. A unit added mid-rename gets `studio-<stem>` and `claude-studio-<stem>` | mixed old and new names |
| Standing Production rules that name `build-followups` ("merge main into and push build-followups") | The rule says "the Merge branch" instead (as `produce.md` already does). The production doc is the showrunner's; it rephrases the rule once, and verify flags a branch literal in live rules | every branch literal in standing rules |
| 123 fixture mentions in 12 test files, renamed to `studio` | Converted once to synthetic names (as `test_rename.py` uses `old`/`new`) | the 12-file fixture row |
| A unit's phase-start merge fetches the merge branch it remembers | The unit resolves its Merge branch through `showrunners.py` (Formerly-aware) at each phase start, so an unmoved unit merges `studio` | stale gates for unmoved units |
| statusline opens `showrunner-$SHOWRUNNER_UNIT/conf` directly | It resolves the slug through the shared resolver first | lost labels on unmoved units |

**Considered and not taken: a permanent production key** for branch and worktree
namespaces (`units/p42/<stem>`) and state paths. My call: no. Branches, worktrees and
state paths are what people read; an opaque key there costs every reader a lookup and
every person a translation, and production renames are rare. The tool does the moves,
one unit at a time. Same call as the unit rename's permanent unit key.

### Order and resume (recorded)

- **Checkout last.** `apply` keeps the `Showrunner checkout` field on the existing
  directory; `finish` moves the directory in its own journaled step that records both
  locations, runs from a stable cwd, rewrites the field and every absolute pointer
  (timer CHECK, journal paths) after the move, and moves the showrunner's own session
  through the stop and relaunch path (`showrunner_move.py`).
- **Merge before move.** A unit's move stops it, merges the new merge branch into its
  branch **in the old worktree** (a conflict aborts and leaves the unit runnable under
  its old name), then moves it. The journal keeps the original tip and the merged tip
  apart; retirement checks the merged tip.
- **Safe point.** `--wait` waits for a phase boundary (phase finished, checkpoint
  merged, clean worktree) before asking the unit to stop; it waits outside the lock.
- **Durable lifecycle.** Before stopping, the journal records the session id, account,
  shutdown request and stage; a retry after relaunch recognises the live new session
  and does not stop it again. Stop, wait and relaunch live in `unit_move` and are
  shared with the unit rename.
- **Writers held off.** The timer instance is paused for `apply` (and its prior state
  restored); stall-watch files move under stall-watch's lock; a stretch the timer
  wrote under the new name meanwhile is merged, keeping the older start.
- **One lock**, shared with the unit rename, outside every renamed directory
  (`~/.local/state/showrunner/rename.lock`); each command refuses while the other's
  journal is unfinished.
- **Untracked logs** follow one policy across `apply`, `unit`, commit and `finish`
  checks: production logs stay untracked and excluded, their content kept.
- **Worklist from the table.** Steps 3 to 6 move every live Units row with an
  old-prefixed branch, read at run time (the survey missed `style-unit`); the builder
  still moves last.

### Places added to the registry

| Place | Verdict |
| --- | --- |
| Recorder state `progress_history_state.json` in each open run's session dir (`/tmp/claude/unit/…`, `/tmp/claude/delegate/…`) | R with the run record, through `progress_history.py`'s own move |
| The newest **finished** run of each unit (`unit_lookup.run_state` reads the newest record for the worktree) | R: location fields moved, so a finished unit still reads finished |
| Hanadocs phase notes owned by `build-followups/<unit>` | D: `phase_table` resolves the production through Formerly before comparing ownership, then `refresh` rewrites the link |
| `.git/worktrees/<name>` admin dirs (git keeps the old name after `worktree move`) | H: git-internal, never read by a name; verify allows them |
| Bevy upgrade watch `BEVY_UPGRADE_STATE_DIR/<slug>.json` and its notifier, when present | R; absent today, a fixture covers it |
| `/showrunner:rename` callers: `produce.md`, `attach.md`, `add_showrunner.md`, `production_format.md` | R in the command-rename commit |
| Production rules prose | live (above) |

### Verify (recorded)

Each registry place is an executable entry: its enumerator (git refs through git,
paths by name and by content, hashed stall paths computed from the old slug, `/tmp`
session dirs), its rewrite and its postcondition (new value required, old value
allowed, or history). Unreadable required roots fail. A test injects one missed place
per source family and expects a failure.

### Build, as revised

| Phase | What |
| --- | --- |
| 1 | `apply`: the log policy; title and Log lines (checkout field stays); plan files to work names with link rewrites; timer fields from the shared generators with the instance paused; history.jsonl; dailies keys; the Bevy watch; the shared lock; fixtures to synthetic names (G1-G6) |
| 2 | `unit` on `unit_move`'s shared lifecycle: merge before move, stop and relaunch with journaled stages, `--wait` at a phase boundary, any process cwd inside refuses, the open and newest finished run with recorder state, shutdown record, worklist from the Units table (G7-G10) |
| 3 | `finish`: the checkout handoff and the showrunner's own move; orphan clean-up; `verify` on the executable registry; Formerly-aware phase notes, statusline and phase-start merge; the command rename with its callers; the as-built (G11-G12) |
| 4 | Live check on `fake_showrunner.py`, then steps 0-9 on build-followups |

## Calls I made (each a one-line revert)

- Extend `rename.py` rather than build a sibling; rename the command to `rename_production`.
- Retire the three worktree-less rows (runs done) so finish can complete.
- Delete merged orphan branches and worktrees instead of renaming them.
- Rename the three `build-followups*` as-built docs by their content, not to `studio-*`.
- ~~Rename every `docs/plans/build-followups-*` file to `studio-*`.~~ Superseded by the team review: plans take their work's name with no slug.

## Questions for you

None. The name is yours; everything else above is settled.
