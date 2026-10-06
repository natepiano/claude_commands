# Showrunner enhancements: fewer steps to remember

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Scripts for the showrunner's mechanical steps, so a showrunner starts a unit and merges a checkpoint with one command each, and every other step it must remember gets a script-or-not verdict.

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06:
- 14:0x PDT, on the steps a showrunner runs to start a unit: "okay - that's too many steps for a producer to have to remember - how can we automate this - and allow for both a situation where a doc is written and where one is NOT written but instead should be written by the unit director - and even a unit that just needs to sit there and be ready to go".
- 14:1x PDT: "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember".
- 14:2x PDT: "move hook phase 7 to its own new unit called enh-showrunner - setting it up as you see fit".

Both phases were Phases 7 and 8 of stalls-unit's plan (`docs/plans/build-followups-fn-length-hook.md`). The showrunner moved Phase 8 here with Phase 7: both script the showrunner's own steps and share the production doc reader.

## Decisions (showrunner)

- **Gate G1:** Phase 2 starts after stalls-unit Phase 6 merges (ETA 16:15 PDT). That phase changes `produce.md`, `promote_unit.md`, `showrunners.py` and `stall_watch.py`, which Phase 2 builds on.
- **Split (unit director, 2026-10-06 14:5x PDT):** the command's own files touch none of those four, so Phase 1 runs now and ships `--plan` and `--brief`; standby and the hub-file edits are Phase 2, after G1; the checkpoint merge is Phase 3. Production rule "parallel by default" (`produce.md` → Rules, `ed166b6`).
- **Hub files:** `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py` and `scripts/production/stall_watch.py` are stalls-unit's; this unit edits them after G1. Merge `build-followups` into the branch before each phase.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds `/showrunner:add_unit` (Phases 1 and 2) and `merge_checkpoint.py` with the script-or-not audit (Phase 3). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
- **Project started:** 2026-10-06T21:25:00+00:00
- **Stack:** Python 3.13, standard library only; zsh for command lines.
- **Layout:**
  - `scripts/production/` — production scripts and their `test_*.py`
  - `commands/showrunner/` — the showrunner commands
- **Key files:** `commands/showrunner/produce.md` (<LaunchUnits/>, <MergeCheckpoint/>), `commands/showrunner/promote_unit.md`, `docs/production_format.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `scripts/production/unit_status.sh`, `scripts/production/review_regime.py`, `docs/plans/build-followups-production.md` (a real production doc to read, never write).
- **Port:** none.
- **Test lane:** `scripts/production/`.
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - Times state PDT.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 | stalls-unit Phase 6 | the showrunner merges it and says so |

## Phases

### Phase 1 — One command starts a unit, with a plan or with a brief for its unit director to plan · status: done

#### As-built

`/showrunner:add_unit` runs `add_unit.py --production <doc> <name> (--plan <path> | --brief <words>) [--port N] [--owns <paths>] [--resume <sessionId> --cwd <dir>] [--timeout S]`. One run, restartable from any step: preflight; for a brief, a stub plan holding the user's words under `## Source`; the Units row; commit `production(<slug>): add unit <unit> (<plan|brief>)` and push of the merge branch; the worktree and branch, pushed with upstream; a tmux session under `systemd-run --user --scope` running `claude --remote-control <name>` with the unit's prompt; a wait for `/remote-control is active`; `showrunners.py add`; the unit added to an old-form `prompt.txt`; one LOG line. A re-run skips every step already done. `--resume` reattaches an existing session in its own cwd and tells it, with the plan's absolute path, that it is now the unit.

- The request is typed: `Production` (doc, slug, merge_branch, checkout, showrunner_session, log, zone), `PlanGiven | BriefGiven`, `NewSession | ResumedSession`, `UnitLaunch`.
- `read_production(path) -> Production` and `production_field(lines, field) -> str` are the production-doc reader.

**Files:**
- `scripts/production/add_unit.py` — the command.
- `scripts/production/test_add_unit.py` — subprocess tests with fake `claude`, `tmux`, `systemd-run` and `nix` on `PATH` and a temporary bare origin.
- `commands/showrunner/add_unit.md` — the skill.

**Binds later work:** the standby state joins `UnitLaunch.plan` beside `PlanGiven | BriefGiven`, and `recorded_mode` accepts its commit-subject mode; the merge-checkpoint script imports `read_production` and `production_field`.

**Gotchas:**
- tmux targets are `=<name>`; a plain `-t <name>` prefix-matches another session.
- A re-run must name the mode it was started with; the mode is read from the commit subject, then from the stub's `## Source`.
- Every refusal happens in `preflight`, before any write: another unit's branch or worktree in the Units table, an existing local or origin branch, an occupied worktree path, a live tmux session, a plan missing on the merge branch or lacking the Production header, a stub with different words.

**Ruled out:** a mode column in the Units table — the commit subject already records it.

### Phase 2 — A unit can wait on standby, and promote and produce use the command · status: todo

**Blocked by:** G1 — stalls-unit phase 6 merged into `build-followups`

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:0x PDT, through the showrunner (natedev): "... and even a unit that just needs to sit there and be ready to go". The rest of the original Phase 1, split out by the unit director 2026-10-06 14:5x PDT because it edits the four hub files stalls-unit Phase 6 changes.

**Goal:** `/showrunner:add_unit <name> --standby` starts a unit that does nothing until the showrunner sends it work, and the stall watcher never bumps it while it waits. `promote_unit.md` uses `add_unit.py --resume` for its launch and record steps, and `produce.md` <LaunchUnits/> shrinks to the command.

**Spec:**
- **Standby mode:** `add_unit.py` and `add_unit.md` gain `--standby`, a third named state `Standby` beside `PlanGiven` and `BriefGiven`; exactly one of the three. It writes no plan; the row's Plan cell reads `standby`; the commit subject ends `(standby)`; the prompt is `You are <name>-unit in production <slug> (doc <path>), under the showrunner <session>, on standby. Work only in your worktree <worktree>, branch <branch>. Do nothing until the showrunner sends you work.`; step 7 passes `--standby` to `showrunners.py add`; the LOG line says `standby`.
- **The registry:** `showrunners.py add` gains `--standby`, which records the unit in that showrunner's `standby` list; `showrunners.py ready <session> --unit <name>` takes it out, and the showrunner runs it when it hands the unit work, changing the Units row's Plan cell from `standby` to the plan. The config reader converts each unit into a named state (working or standby) at the boundary. `stall_watch.py` never bumps or reports a standby unit.
- **Promote:** `promote_unit.md` keeps steps 1, 2 and 5 (find it, check fit, stop it) and replaces steps 3, 4, 6 and 7 with one call of `add_unit.py --resume <sessionId> --cwd <cwd> --plan <plan>`.
- **Produce:** <LaunchUnits/> becomes the command, one line per unit to start, plus what stays the showrunner's own: typing `/compact` into a unit director blocked on a full context, and **Resume** (step 4). <StartRun/>'s worktree and launch steps point to it.

**Files:**
- `commands/showrunner/add_unit.md` — `--standby`.
- `scripts/production/add_unit.py` — the `Standby` state; `scripts/production/test_add_unit.py`.
- `scripts/production/showrunners.py` — `--standby` and `ready`; `scripts/production/test_showrunners.py`.
- `scripts/production/stall_watch.py` — skips a standby unit; `scripts/production/test_stall_watch.py`.
- `commands/showrunner/produce.md` — <LaunchUnits/> shrinks to the command.
- `commands/showrunner/promote_unit.md` — steps 3, 4, 6 and 7 call the script.

**Seats:** 1 writer + 1 tester.
- `impl` — `commands/showrunner/add_unit.md`, `scripts/production/add_unit.py`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`; post `done` without waiting for the test seat.
- `test` — the standby cases in `scripts/production/test_add_unit.py`, `scripts/production/test_showrunners.py` and `scripts/production/test_stall_watch.py`, from the Spec alone; owns the final suite run:
  - `--standby`: the row's Plan cell reads `standby`, the config lists the unit as standby, no plan is written, the prompt is the standby one, and the stall watcher skips the unit while it stays idle; after `ready` it is bumped as any unit;
  - `--standby` with `--plan` or `--brief` exits 2 with no change;
  - `showrunners.py ready` on a unit that is not standby changes nothing and says so.

**Constraints from prior phases:**
- Phase 1 (as built): `add_unit.py` and its typed launch request; add the state, never a parallel path. `UnitLaunch.plan` is `PlanGiven | BriefGiven` — `Standby` joins that union; `recorded_mode` reads the mode from the commit subject `production(<slug>): add unit <unit> (<mode>)` and must accept `standby`; `prompt_for` gains the standby prompt; every refusal stays in `preflight`, before any write; tmux targets are `=<name>`.
- stalls-unit Phase 5 (as built): every edit of `config/showrunners.json` goes through `showrunners.py`'s locked write (`change()`), and `stall_watch.py` reads units from it.
- stalls-unit Phase 6 (as built at G1; it also makes `stall_watch.py` find a unit whose pane process is `claude` itself): `showrunners.py rename` and `import` of both prompt forms, `unit_status.sh … --showrunner <session>`, the `tmux-names` instance; build on them, never a parallel copy.
- Tests never start a real `claude`, `tmux` or `systemd-run`, never write the real `~/.claude/config/showrunners.json`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'`, `-p 'test_showrunners.py'` and `-p 'test_stall_watch.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): `/showrunner:add_unit add-scratch --standby` prints the attach line, the pane shows remote control active, `showrunners.py list` shows `add-scratch` standby, and the stall watcher does not bump it over 6 idle minutes. Then remove it as in Phase 1's live gate.

### Phase 3 — One command merges a checkpoint, and every other showrunner step gets a script-or-not verdict · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:1x PDT, through the showrunner (natedev): "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember". The script, its steps and the audit are the showrunner's packaging of that ask, placed here as Phase 2, and Phase 3 since the 14:5x PDT split.

**Goal:** `merge_checkpoint.py --production <doc> <unit> <phase> <hash> [--also <path>…]` runs `produce.md` <MergeCheckpoint/>'s mechanical steps in one run, prints one result line per step and the message to send the unit, and on red leaves the merge branch as it was; <MergeCheckpoint/> keeps only the judgment steps and the call. The As-built lists every other step in `produce.md` and `commands/showrunner/dailies.md` the showrunner must remember, each with a script-or-not verdict, and each clear one becomes a follow-up phase.

**Spec:**
- **Reads** from the production doc what <MergeCheckpoint/> reads: `MERGE_BRANCH`, `CHECKOUT`, `LOG`, `ZONE`, the unit's Units row (branch, Owns), the hub-file rows, **Merge tests**, and the production rules that decide the push. `LAST_MERGED[unit]` comes from the merge subjects on `MERGE_BRANCH` (`Merge <unit> phase <N> (<short>) into <merge branch>`), never from an argument. Two optional doc fields make the push explicit: **Push:** `validate_and_push` (the default, <MergeCheckpoint/> step 10) or `git`; **Promote:** `<checkout>[, mac <path>]` (after a push, fast-forward that checkout's `main` and pull on the Mac). This production's doc carries `**Push:** git` and `**Promote:** ~/.claude, mac ~/.claude` (the showrunner, 2026-10-06). **Known flakes:** names packages whose red-then-green-alone run counts green.
- **Steps,** each printing `<step>: ok|held|failed — <one line>`; the first `held` or `failed` stops the run:
  1. ancestry (`cat-file -e`; `merge-base --is-ancestor LAST_MERGED <hash>`, skipped on a unit's first merge) and on-origin (`fetch origin <branch>`, `--is-ancestor <hash> origin/<branch>`; failing prints the message asking the unit to push);
  2. scope: every path of `diff --name-only <merge branch>...<hash>` is in Owns, a hub row of this unit, or an `--also` path; anything else is `held` with the paths and the message asking the unit why;
  3. conflicts: `merge-tree --write-tree --name-only` exit 1 is `held` with the message asking the unit to merge the merge branch and send a new hash;
  4. other units (data only, never a stop): for each other unit, the overlap of this change's paths with its branch diff and its worktree's `status --short`, printed so the showrunner applies <CrossUnitChange/> or the one-line notice;
  5. merge: the message file `<scratch>/merge_<short>.msg` as <MergeCheckpoint/> step 7 writes it, with this session's attribution lines passed by `--trailer`, then `merge --no-ff -q -F`;
  6. test: each package owning a changed file (nearest `Cargo.toml`) through `verify.sh test`, each changed example through `verify.sh example`, then each **Merge tests** command, one after another into `<scratch>/merge_<short>_test.log` with a `<name>_EXIT=<rc>` line each;
  7. red: each red package rerun once alone; green alone and listed in **Known flakes** continues with a LOG note; otherwise `reset --keep HEAD~1` after checking `HEAD` is this unpushed merge, and print the message with the failing tests and the log path;
  8. green, `Push: git`: merge `origin/main` with `--no-ff` when it has diverged, push `MERGE_BRANCH`, then each **Promote**: `git -C <checkout> merge --ff-only <merge hash>` (other sessions' uncommitted files left alone; a refusal is `failed` naming the paths, nothing undone), push main only when `origin/main..main` held no other session's commits, then the Mac's `git pull --ff-only` over `ssh mac`, read by the `rc=` it prints (ssh's own status is always 0); `Push: validate_and_push`: <MergeCheckpoint/> step 10's command and its undo;
  9. record: `review_regime.py add` with the numbers from `--review-trial "<the notice's line>"`, `--holds` and `--merge-defects` (default 0), `--started`, and the merge time; then the LOG line `- HH:MM <zone>: <unit> phase <N> (<hash>) merged as <merge hash>; <tests> green; pushed[; promoted]`.
- The last line printed is `send <unit>: <message>`: the merged line with the next step, or the hold or failure and what the unit does.
- `commands/showrunner/produce.md` <MergeCheckpoint/>: steps 1–3 and 7–10 and the record step become the call, run in the background; steps 4–6 (other units, new public items, design check), <ClearGate/>, <CrossUnitChange/>, <CIPoint/> and `review_regime.py watch` stay the showrunner's, read from the script's lines.
- **The audit:** every other step in `produce.md` and `commands/showrunner/dailies.md` the showrunner must remember — building the dailies input from `unit_status.sh`, the idle check, promotion and the Mac pull included — one row each: the step, where it lives, `script` or `judgment`, and why. Written into this phase's As-built; each `script` row the unit director turns into a follow-up phase with its own Work Order.

**Files:**
- `scripts/production/merge_checkpoint.py` — new.
- `scripts/production/test_merge_checkpoint.py` — new.
- `commands/showrunner/produce.md` — <MergeCheckpoint/> calls the script.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/production/merge_checkpoint.py`, `commands/showrunner/produce.md`; post `done` without waiting for the test seat.
- `test` — `scripts/production/test_merge_checkpoint.py` from the Spec alone, with real `git` in temporary repositories and a temporary bare `origin`, stubs on `PATH` for `verify.sh`, `validate_and_push.sh`, `review_regime.py` and `ssh` recording argv: each step's `held` and `failed` cases with no merge left behind; a red package reset and a known flake continuing; the green `git` path merging diverged `origin/main`, pushing, promoting a second temporary checkout by fast-forward and reading the Mac's `rc=`; a promote refused by an uncommitted file fails without undoing the push; the record call's argv and the LOG line. Owns the final suite run. Then the audit table, in its summary.

**Constraints from prior phases:**
- Phase 1 (as built): `add_unit.py` reads the production doc with `read_production(path) -> Production` (doc, slug, merge_branch, checkout, showrunner_session, log, zone) and `production_field(lines, field) -> str`; import them, never a second parser.
- Phase 2 (as built at its merge): it edits `produce.md` <LaunchUnits/>; this phase edits <MergeCheckpoint/>. Starts beside Phase 2 once Phase 1 merges, claiming `produce.md` after it.
- Tests never push anywhere but a temporary bare repository, never run real `ssh`, and never write the real `~/.claude` checkout or `~/.local/state/`.
- Only the showrunner merges (production_format item 4): the live gate is natedev's.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): the next checkpoint any unit sends is merged with the script; its lines match what the showrunner would have done by hand, and the As-built carries the audit table.

### Phase 4 — Simple dailies name only the next item; page dailies list each upcoming item as a sub-bullet · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 ~15:10 PDT, typed in this unit's session: "as you can update the dailies so that the simple daily doesn't show then for all upcoming phases but just the next - the dailies page version can show upcoming versions and it should put them as sub bullets not as a comma delimited list". Their example was a Hana unit's `then:` line naming phases 64 to 80 in one comma-separated sentence. The showrunner (natedev, 15:1x PDT) placed it here as Phase 4 and gave this unit `scripts/production/dailies_render.py`, its tests and `commands/showrunner/dailies.md` for this change (stalls-unit's files; `also touches` in the checkpoint notice). It runs after Phase 1's checkpoint, since one run's seats work one phase at a time.

**Goal:** a unit's upcoming work is a list in the dailies input. `/showrunner:dailies simple` prints only its first item, as `- then: <item>`; `page` and `elaborate` print `- then:` with each item as an indented sub-bullet, in order.

**Spec:**
- **Input:** a unit's `then` (`scripts/production/dailies_render.py`, read at about line 670) is a JSON list of non-empty strings, one item each (`["64: front output jacks start a cable from a press at their centre", "65: jack panels match the info panels' colour and border"]`). A plain string is still read, as a one-item list, so a showrunner mid-run on the old form is not refused. The rules that read `then` today hold per item: `check_then_order` (about line 564) checks each item's phase numbers against the heading's, and the follow-up and last-phase rules (about lines 677–679) apply to the list as a whole. The reader converts the input into a named type at the boundary (`UpcomingWork`, holding a non-empty tuple of items, or its absence named as such), never a bare `str | None`.
- **Render** (about line 1121): `simple` prints `- then: <first item>` and nothing for the rest. `page` and `elaborate` print `- then:` then each item on its own line as `  - <item>`. A one-item list prints `- then: <item>` at every length.
- **`commands/showrunner/dailies.md`:** the input schema and its example (about line 119) show `then` as a list, one item per upcoming phase or follow-up, each led by its phase number when it has one. A range of phases with one shared purpose is one item (`76–79: edge cases in selection, jack panels, the palette and Log, saved scenes and reset`). The note at about line 167 on impossible orders reads per item.

**Files:**
- `scripts/production/dailies_render.py` — the list input, the named type, the per-length render.
- `scripts/production/test_dailies_render.py` — the cases below.
- `commands/showrunner/dailies.md` — the schema, example and order note.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/production/dailies_render.py`, `commands/showrunner/dailies.md`; post `done` without waiting for the test seat.
- `test` — `scripts/production/test_dailies_render.py`, from the Spec alone; owns the final suite run:
  - a three-item `then`: simple prints only the first item; page and elaborate print `- then:` and three sub-bullets in order;
  - a one-item list and a plain string both print `- then: <item>` at every length;
  - an empty list, or an item that is empty, is refused naming `then`;
  - an item naming a phase before the heading's is refused as today, and the last-phase rule still requires `then`.

**Constraints from prior phases:**
- stalls-unit owns these files; it has no edits to them committed or in its worktree (the showrunner, 15:1x PDT). If its Phase 6 lands an edit to `dailies_render.py` first, merge `build-followups` and resolve here.
- Every other dailies test (`test_dailies_render_agents.py`, `test_dailies_render_holds.py`) stays green.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/dailies_render.py` and `scripts/production/test_dailies_render.py`.
