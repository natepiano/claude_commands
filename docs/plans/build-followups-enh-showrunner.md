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

- **Gate G1:** Phase 1 starts after stalls-unit Phase 6 merges (ETA 15:45 PDT). That phase changes `produce.md`, `promote_unit.md`, `showrunners.py` and `stall_watch.py`, which Phase 1 builds on. Until then, read the code and validate the Work Orders.
- **Hub files:** `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py` and `scripts/production/stall_watch.py` are stalls-unit's; this unit edits them after G1. Merge `build-followups` into the branch before each phase.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds `/showrunner:add_unit` (Phase 1) and `merge_checkpoint.py` with the script-or-not audit (Phase 2). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
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
| G1 | Phase 1 | stalls-unit Phase 6 | the showrunner merges it and says so |

## Phases

### Phase 1 — One command starts a unit: with a plan, with a brief for its unit director to plan, or on standby · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:0x PDT, through the showrunner (natedev), on the steps a showrunner runs to start a unit (`produce.md` <LaunchUnits/> and `promote_unit.md`'s shared steps): "okay - that's too many steps for a producer to have to remember - how can we automate this - and allow for both a situation where a doc is written and where one is NOT written but instead should be written by the unit director - and even a unit that just needs to sit there and be ready to go". The command, its modes and the acceptance gate are the showrunner's packaging of that ask, placed here as Phase 1.

**Goal:** `/showrunner:add_unit <name> (--plan <path> | --brief "<goal in the user's words>" | --standby)`, run in a showrunner's session, starts a unit in one run — its Units row, the merge branch committed and pushed, its worktree and branch, its unit director in a detached tmux session outside the showrunner's scope, the check that remote control is active, the registry and a LOG line — and prints `tmux attach -t <name>`. A standby unit is never bumped by the stall watcher. `promote_unit.md` uses the same script for its launch and record steps, and `produce.md` <LaunchUnits/> shrinks to the command.

**Spec:**
- **The skill:** `commands/showrunner/add_unit.md` is `/showrunner:add_unit <name> (--plan <path> | --brief "<goal>" | --standby) [--port <n>] [--owns "<text>"]`. It runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <PRODUCTION_DOC> <its arguments>` and shows what the script printed; on a non-zero exit it shows the message and stops. Exactly one of `--plan`, `--brief`, `--standby`.
- **What the script reads.** From `--production <doc>`: `MERGE_BRANCH` (**Merge branch**), `CHECKOUT` (**Showrunner checkout**), the showrunner session (**Showrunner session**), `LOG` (**Log**, relative to `CHECKOUT`), `ZONE` (**User zone**), the Units table, and the slug (the doc's file name less `-production.md`). From `<name>`: the session and tmux name `<name>`, the unit `<name>-unit`, the branch `<MERGE_BRANCH>-<kebab>` and the worktree `<parent of CHECKOUT>/<CHECKOUT's base name less a trailing -trunk>-<kebab>`, where `<kebab>` is `<name>` with `_` turned into `-` (so `mul_add` gives `build-followups-mul-add`, as the existing rows do). These values become one typed launch request at the boundary; a mode is a named state (`PlanGiven(path)`, `BriefGiven(words, stub path)`, `Standby`), never a set of optional strings.
- **Refusals, before any change:** exit 2 with one line when `CHECKOUT` is not on `MERGE_BRANCH`; when the name is already a Units row other than one this script wrote for the same name and mode (see re-runs); when a tmux session `<name>` is live with no unit director this script started; when `--plan`'s file does not exist on `MERGE_BRANCH` or lacks the `> **Production:**` header; or when the doc lacks a field above.
- **The steps, in order; each one first checks whether it is already done, so a re-run with the same arguments after a failure finishes the rest and repeats nothing:**
  1. **Plan.** `--brief` writes the stub plan `docs/plans/<MERGE_BRANCH>-<kebab>.md` in `CHECKOUT`: a `# <name>` title, the line `> **Production: <slug>** — unit `<name>-unit`; production doc `<doc path relative to CHECKOUT>``, and `## Source` holding the user's words verbatim with the date and time in `ZONE`. `--plan` uses the given path; `--standby` writes none.
  2. **Row.** Append `| <name>-unit | <plan path, or standby> | <worktree> | <branch> | <name> | <port or —> | <owns or —> |` to the Units table.
  3. **Commit and push** in `CHECKOUT`, staging only the doc and the stub plan: `production(<slug>): add unit <name>-unit (<plan|brief|standby>)`, then `git push origin <MERGE_BRANCH>`.
  4. **Worktree:** `git -C CHECKOUT worktree add <worktree> -b <branch> <MERGE_BRANCH>`, `git -C CHECKOUT push -u origin <branch>`, and, when `CHECKOUT/.claude/config/berth.toml` exists, `git -C CHECKOUT config branch.<branch>.cargoBerthTarget <MERGE_BRANCH>`.
  5. **Launch,** exactly as <LaunchUnits/> step 2 does by hand today: tmux is `command -v tmux`, else `$(nix build --no-link --print-out-paths 'nixpkgs#tmux^out')/bin/tmux`; run under `systemd-run --user --scope --unit=<name>`, with every `CLAUDE_*` variable removed from the environment; `tmux new-session -d -s <name> -c <worktree> -e SHOWRUNNER_UNIT=<slug> zsh -ic "ENABLE_TOOL_SEARCH=true command claude --remote-control <name> -n <name> --settings '{\"disableAgentView\": true}' '<prompt>'; exec zsh"`. The prompt by mode: `--plan` is `/unit:delegate <plan>`; `--brief` is `You are <name>-unit in production <slug> (doc <path>), under the showrunner <session>. Work only in your worktree <worktree>, branch <branch>, and name it in every Work Order. Your plan <stub path> holds only the user's words. Write the full phased plan there, send it to the showrunner, and wait for its approval before you run /unit:delegate <stub path>.`; `--standby` is `You are <name>-unit in production <slug> (doc <path>), under the showrunner <session>, on standby. Work only in your worktree <worktree>, branch <branch>. Do nothing until the showrunner sends you work.`
  6. **Check:** read the pane (`tmux capture-pane -p -t <name>:`) until it shows `/remote-control is active`, at most `--timeout` seconds (default 90). On timeout exit 1 with the session name and the pane's last 15 lines; the steps after this one do not run, and the tmux session stays for the user to read.
  7. **Record:** `showrunners.py add <showrunner session> --zone <ZONE> --unit <name>`, with `--standby` for a standby unit; when `PROMPT_FILE` (`~/.local/state/showrunner/<slug>/prompt.txt`) still holds the old `unit_status.sh <scratch> <zone> <session…>` form, add `<name>` to that list in one atomic rewrite (the `--showrunner` form reads the config and needs nothing); append `- HH:MM <zone>: added <name>-unit (<plan|brief|standby>), tmux <name>, worktree <worktree>` to `LOG`.
  8. Print `<name>-unit started: tmux attach -t <name>`.
- **Promote reuses it:** `add_unit.py --resume <session id> --cwd <dir>` (with `--plan`) skips nothing but the stub and replaces step 5's command with `claude --resume <session id> --remote-control <name> -n <name> --settings '{\"disableAgentView\": true}' '<promote prompt>'` started in `<dir>`, the prompt being `promote_unit.md` step 6's. `promote_unit.md` keeps steps 1, 2 and 5 (find it, check fit, stop it) and replaces steps 3, 4, 6 and 7 with that one call.
- **Standby:** `showrunners.py add` gains `--standby`, which records the unit in that showrunner's `standby` list; `showrunners.py ready <session> --unit <name>` takes it out, and the showrunner runs it when it hands the unit work, changing the Units row's Plan cell from `standby` to the plan. The config reader converts each unit into a named state (working or standby) at the boundary. `stall_watch.py` never bumps or reports a standby unit.
- **Produce:** <LaunchUnits/> becomes the command, one line per unit to start, plus what stays the showrunner's own: typing `/compact` into a unit director blocked on a full context, and **Resume** (step 4). <StartRun/>'s worktree and launch steps point to it.

**Files:**
- `commands/showrunner/add_unit.md` — new: `/showrunner:add_unit`.
- `scripts/production/add_unit.py` — new: the steps above.
- `scripts/production/test_add_unit.py` — new.
- `scripts/production/showrunners.py` — `--standby` and `ready`; `scripts/production/test_showrunners.py`.
- `scripts/production/stall_watch.py` — skips a standby unit; `scripts/production/test_stall_watch.py`.
- `commands/showrunner/produce.md` — <LaunchUnits/> shrinks to the command.
- `commands/showrunner/promote_unit.md` — steps 3, 4, 6 and 7 call the script.

**Seats:** 1 writer + 1 tester.
- `impl` — `commands/showrunner/add_unit.md`, `scripts/production/add_unit.py`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`; post `done` without waiting for the test seat.
- `test` — `scripts/production/test_add_unit.py`, the standby cases in `scripts/production/test_showrunners.py` and `scripts/production/test_stall_watch.py`, from the Spec alone; owns the final suite run. Real `git` in temporary repositories with a temporary bare `origin`; stub `tmux`, `systemd-run`, `nix` and `claude` on `PATH`, each recording its argv and environment; a temporary `HOME`, production doc, `LOG` and config:
  - `--plan`: the row, one commit holding only the doc, the merge branch and the new branch pushed to the bare origin, the worktree on the new branch from the merge branch; `cargoBerthTarget` set only when `berth.toml` exists; `systemd-run` gets `--user --scope --unit=<name>`; `tmux new-session` gets `-s <name> -c <worktree> -e SHOWRUNNER_UNIT=<slug>` and an environment with no `CLAUDE_*` variable; the command ends `'/unit:delegate <plan>'`; the config gains the unit; one LOG line; the printed attach line;
  - `--brief`: the stub plan carries the Production header and the user's words verbatim and is in the commit; the prompt asks for the full plan, the showrunner's approval, then `/unit:delegate <stub>`;
  - `--standby`: the row's Plan cell reads `standby`, the config lists the unit as standby, no plan is written, and the stall watcher skips the unit while it stays idle; after `ready` it is bumped as any unit;
  - the pane never shows `/remote-control is active`: exit 1 within the timeout naming the session, with no config entry and no LOG line; a re-run once the stub pane shows it does not add a second row, commit or worktree, and finishes;
  - refusals exit 2 with no change: `CHECKOUT` off `MERGE_BRANCH`, a taken name, a live tmux session of that name, a `--plan` file missing or without the header, two modes at once;
  - `--resume` starts `claude --resume <id>` in `--cwd` with the promote prompt;
  - `<name>` with `_` gives the kebab branch and worktree; a checkout not ending in `-trunk` gives `<checkout>-<kebab>`;
  - the old prompt form gains the session; the `--showrunner` form is left alone.

**Constraints from prior phases:**
- stalls-unit Phase 5 (as built): every edit of `config/showrunners.json` goes through `showrunners.py`'s locked write (`change()`), and `stall_watch.py` reads units from it.
- stalls-unit Phase 6 (as built at G1; it also makes `stall_watch.py` find a unit whose pane process is `claude` itself): `showrunners.py rename` and `import` of both prompt forms, `unit_status.sh … --showrunner <session>`, the `tmux-names` instance; build on them, never a parallel copy.
- Tests never start a real `claude`, `tmux` or `systemd-run`, never write the real `~/.claude/config/showrunners.json`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- The merge branch is the showrunner's (production_format item 4): only the showrunner runs this command, so the unit director does not run the live gate.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'`, `-p 'test_showrunners.py'` and `-p 'test_stall_watch.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): `/showrunner:add_unit add-scratch --standby` prints the attach line, the pane shows remote control active, `showrunners.py list` shows `add-scratch` standby, and the stall watcher does not bump it over 6 idle minutes. Then remove it: kill its tmux session, `showrunners.py remove natedev --unit add-scratch`, delete its Units row and commit, `git worktree remove` its worktree, and delete its branch locally and on origin.

### Phase 2 — One command merges a checkpoint, and every other showrunner step gets a script-or-not verdict · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:1x PDT, through the showrunner (natedev): "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember". The script, its steps and the audit are the showrunner's packaging of that ask, placed here as Phase 2.

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
- Phase 1 (as built): `add_unit.py` reads the production doc; share its reader, never a second parser.
- Tests never push anywhere but a temporary bare repository, never run real `ssh`, and never write the real `~/.claude` checkout or `~/.local/state/`.
- Only the showrunner merges (production_format item 4): the live gate is natedev's.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): the next checkpoint any unit sends is merged with the script; its lines match what the showrunner would have done by hand, and the As-built carries the audit table.

