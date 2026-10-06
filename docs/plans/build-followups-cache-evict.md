# cache-evict: the disk floor takes from the least used build cache first

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; approved by the showrunner 2026-10-06 (Phase 2 `--forecast` dropped, the re-measure renumbered Phase 2).** Below its free-space floor, the disk floor sweep removes only the shortfall, but today it spreads that across every idle target by compile age, so each active unit loses part of its working set. This plan makes it take from the least recently used target first, records each target's last build so a test rerun counts as use, names in the journal whose cache went, and re-measures a day later.

> **Production: build-followups** — unit `cache-evict-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-06 15:29 PDT

have that unit take on this question:

"Trial merge, not built: I trial-merged Phase 13 against the current work of trunk, the widget unit and fps. All three merge without conflicts. I didn't build or test those merges: the disk is at its 500 GB floor, and a second build folder would trigger the cleanup that deletes every unit's build caches. The notice says so."

i.e. figuring out the safest buld cache to delete in such situations so we don't simply get rid of everything but we maybe get rid of the least used to get us under our threshold

## What exists today (measured 2026-10-06 15:00–15:40 PDT by the plan author)

- **The cleanup is `scripts/lint/sweep.py`'s floor mode, `hold_floor()`.** While `/` has less free than `sweep_free_floor_gib.natedev=500` (`config/lint.conf:74`), it removes build output from every cargo target directory under `FLOOR_ROOTS` (`~/rust`, `~/.local/state`, `/tmp`) that no build holds. It runs after every build step (`invoke.sh` `sweep_after_step`, at most once per 5 minutes per working directory, `main()` calling `hold_floor` after the workspace sweep) and every 2 minutes from `/etc/nixos/modules/linux/disk-floor.nix` (`--floor-only`). CI's targets are not under `FLOOR_ROOTS`.
- **It removes only the shortfall, not every cache.** `hold_floor` sets `budget = total - (floor - free)` and `shrink()` → `choose()` removes groups until the idle total fits that budget. A build that writes N GiB below the floor costs about N GiB of other output. The hana unit's claim, and its source, are wrong as worded: `commands/unit/delegate.md:192–193` (the <ToolingContract/> notice) says the sweeper keeps 500 GiB free "by deleting every unit's build caches".
- **The order is global by build unit, and within a day it is compile age, not use.** `choose()` sorts every idle group of every target by `(-whole days since last use, newest compile mtime)`. `last_used` is the newest atime-or-mtime in the unit's `.fingerprint/<unit>/`. `/` is mounted `rw,relatime`, so an atime moves at most once a day after its first read. In a scratch crate, a no-op `cargo build`, `cargo check` and `cargo test --no-run` changed no mtime or ctime anywhere under `target/` (only atimes). So among units touched in the last day the sweep removes the oldest compile first: a dependency compiled this morning and read by every build since goes before a variant compiled an hour ago and never read again.
- **The floor has already drained every target not used today.** A read-only scan at 15:31 PDT (no locks, 2 min 53 s): 40 target dirs, 654.9 GiB; 25 hold nothing removable (`~/rust/hana/target` among them); the oldest remaining unit anywhere was last used at 12:41 PDT. The 15 that hold output, oldest last use first: `bevy_brp_0.20.0-rc1` 36.0 GiB (12:46), `bevy_brp` 27.6 (12:59), two cargo-port scratch crates (14:32, 14:42), `cargo-port-cleanup` 18.7 (14:43), `cargo-liner` 29.7 (15:17), `cargo-tile-enh` 10.9 (15:26), then the hana units at 15:30–15:33: `hana_catalyst` 71.9, `tool-based-ui-geometry-material` 74.3, `tool-based-ui-demo` 85.5, `cargo-liner-berth-flake` 10.6, `tool-based-ui-frame-time` 74.2, `tool-based-ui-trunk` 68.1, `tool-based-ui-startup-polish` 68.4, `widget-enhancements` 76.9.
- **Simulated on that scan, for a 60 GiB shortfall** (a trial build's size): today's order takes from 6 targets, 3 of them hana units in active use (`frame-time` 15.3 GiB, `demo` 11.3, `trunk` 1.9, beside `bevy_brp_0.20.0-rc1` 16.6, `bevy_brp` 10.8, `cargo-port-cleanup` 4.0). Least-recently-used-target-first takes from `bevy_brp_0.20.0-rc1` 16.6, `cargo-liner` 16.5, `cargo-port-cleanup` 16.3 and `bevy_brp` 10.8, and no hana unit. At 100 GiB the target order reaches `hana_catalyst` (19.8); today's reaches five hana units.
- **The floor removes far more than the cache holds, every day.** The 2-minute timer alone (`journalctl --user -u disk-floor.service`) removed 751.7 GiB on 2026-10-04 (103 sweeps), 328.7 on 10-05 (69) and 801.0 on 10-06 by 18:23 EDT (135). Build-log sweep steps (budget and floor together, `steps.sweep_freed_bytes`) freed another 1,133.2 GiB on 10-06. Since 03:00 EDT today the newest unit each timer sweep took was last used a median 3.0 h earlier (min 0.9, max 8.2). Output that active units rebuild minutes later is what goes.
- **A trial merge needs no second build folder.** `docs/production_format.md` rule 9 trial-merges in the unit's own worktree (`git merge --no-commit --no-ff`, test, `git merge --abort`), so only the changed crates rebuild, in that worktree's own target.
- **No per-target use record exists.** The build log (`~/.local/state/buildlog/index.sqlite`, `steps.worktree`, UTC) knows each worktree's last step, but not its target directory, and `sweep.py` never imports `scripts/buildlog` (`disk.py` imports `sweep`). `sweep_workspace()` already knows its targets from `cargo metadata` (`cargo_roots()`) on every per-step sweep.
- **Who parses the sweep's lines:** `scripts/buildlog/parse.py:49–52` counts freed bytes from lines matching `removed N orphaned files (X)`, `removed N build units and M incremental dirs (X)` and `removed … (X), rebuilt by the next doc run`. A new line must not contain `removed`, or the build log counts its bytes twice.

## Decisions (unit director)

- **Target first.** Below the floor, removable output is ordered by its target's last use, oldest target first, and inside a target by today's key. The shortfall still sets how much goes, so the least used target loses output until the floor is met, then the next one; a target used minutes ago goes last. This is the user's rule ("get rid of the least used to get us under our threshold") at the granularity a unit owns: one worktree's target.
- **A target's last use** is the newest of its use stamp's mtime and its units' `last_used`. The stamp `.lint-sweep-used` at the target root is written by every workspace sweep that is not a dry run, before its lock check, so a step that ran in that workspace counts even when it compiled nothing; the units' times cover builds outside `lint` that compiled something. `--target-dir` (CI) and `--dry-run` write no stamp.
- **The workspace budget sweep keeps today's order.** Its groups share one target, so the target key is constant there; nothing in it changes.
- **One line per target in every floor removal**, so the journal names whose cache went and how recently it was used: `lint sweep: the floor took <GiB> from <target>, last used <when>` (`would take` on a dry run). No `removed` in it (parse.py).
- **Every build goes ahead, trial merges included** (showrunner, 2026-10-06): the floor takes the shortfall from the least recently used target, so a unit director has nothing to consult first; no forecast tool.
- **Wording outside this unit's files goes to the showrunner:** natedev has enh-showrunner-unit correct the <ToolingContract/> sentence in `commands/unit/delegate.md`; Phase 1 sends natedev the header comment for `/etc/nixos/modules/linux/disk-floor.nix`.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan changes the disk floor's eviction order in `scripts/lint/sweep.py` (Phase 1) and re-measures a day after it is live (Phase 2). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict` on branch `build-followups-cache-evict` (unit `cache-evict-unit` of production `build-followups`). Name both in every Work Order.
- **Project started:** 2026-10-06T22:31:00+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/lint/sweep.py` — budget sweep, doc-index prune, disk floor, floor alerts; header docstring holds the policy and its measurements
  - `scripts/lint/test_sweep.py` — its tests; `FloorTests` (helpers `base()`, `hold()`, `cargo_target()`, `unit()`) covers the floor
  - `config/lint.conf` — `sweep_free_floor_gib.natedev` (300 GiB since the user lowered it from 500 on 2026-10-06) and its comment
  - outside the repository: `~/.local/state/lint-sweep/` (floor lock, `floor.json`), `journalctl --user -u disk-floor.service` (EDT), `~/.local/state/buildlog/index.sqlite` (UTC; read only, `?mode=ro`)
- **Key files:** `scripts/lint/sweep.py` (`Group`, `group_roots`, `scan_roots`, `choose`, `shrink`, `hold_floor`, `sweep_workspace`, `main`, header paragraphs "The disk floor" and "Last use"); `scripts/lint/invoke.sh:229–248` (`sweep_after_step`), `:355–369` (`invoke_sweep`); `scripts/buildlog/parse.py:49–52` (the lines it counts); `docs/as-built/build-memory-admission.md` "Disk-floor alerts" (as-built to amend at run end).
- **Test lanes:** `scripts/lint/` — `test_*.py` beside the scripts.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s scripts/lint -p 'test_sweep.py'` from the worktree root. Merge tests (production): `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`.
- **Lint:** `basedpyright scripts/lint/sweep.py scripts/lint/test_sweep.py` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing).
- **Style:** none — not Rust.
- **Invariants:**
  - Never remove output from a target whose cargo locks a build holds; the floor keeps taking every idle target's locks for its whole scan and removal, as now.
  - Never run cargo in another session's worktree; tests build fake targets in temporary directories and never touch a real target, `~/.local/state/lint-sweep` or the build log.
  - The floor removes no more than the shortfall plus the last group's overshoot, as now.
  - CI targets (`CI_TARGETS`, `--target-dir`) behave exactly as before.
  - No new sweep output line contains `removed`.
  - `scripts/buildlog/` is read only for this unit. `commands/unit/delegate.md` and `/etc/nixos` are not this unit's: send their text to natedev.
  - Python is typed throughout with no `Any` and no file-level type ignores.
  - `~/.claude` main is the live configuration; each merged phase reaches it at once, and every build step on natedev runs the new sweep from then on.
  - Times carry their zone: this plan states PDT; natedev's journal and `when()` print EDT; build-log stamps are UTC.
  - Saved run output stays under a few GB: read each run and delete it before the next. A disk-floor sweeper keeps the floor free on `/` (500 GiB; 300 GiB once natedev lands the user's change of 2026-10-06 on `build-followups`) by removing build output, least recently used target first once Phase 1 merges.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 | 24 h after Phase 1 reaches `~/.claude` main (T_live, its As-built) | the clock |

## Phases

### Phase 1 — Below the floor, the least recently used target loses output first · status: done

#### As-built

- **Use stamp.** `USE_STAMP = ".lint-sweep-used"`. `sweep_workspace()` sets its mtime to now in every root `cargo_roots()` returns (creating it empty if missing), before `lock_trees`, so a sweep skipped because a build holds a lock still records the step. A dry run and `--target-dir` (CI) write none; a write failure is ignored.
- **Target last use.** `target_last_use(root, groups) -> float` is the newest of the stamp's mtime (0 when absent) and the target's groups' `last_used`.
- **One order.** `Group.build_tree` (required) and `Group.target_used: float = 0.0`; `Scan.orphans` holds one orphan group per build tree. `hold_floor` sets every group's `target_used` from its target. `choose()` sorts by `(target_used, -whole days since last_used, compiled)` and returns `(group, freed)` pairs. The workspace sweep leaves `target_used` at 0, so its order and output are unchanged.
- **Taken bytes.** `shrink()` returns `(left, failures, taken)`: orphan groups and chosen groups, each with its freed bytes; on a real run a group with any entry that failed to remove is left out. The removal line's `last used <oldest> to <newest>` spans the oldest and newest `last_used` among the chosen groups.
- **Per-target lines.** After the existing removal lines, `hold_floor` prints one line per target that lost output, largest first: `lint sweep: the floor took <gib> from <target>, last used <when(target last use)>` (`would take` on a dry run). Orphan bytes count toward their target. No floor line contains `removed`; the aggregate orphan line is unchanged.

**Live check (read only, 2026-10-06 ~15:50 PDT, 60 GiB shortfall):** 42 targets, 659.1 GiB scanned.
- Target-first order: `/home/natepiano/rust/bevy_brp_0.20.0-rc1/target` 35.8 GiB (last used 12:46 PDT), `/home/natepiano/rust/bevy_brp/target` 23.0 GiB (12:59 PDT). Nothing else.
- Prior global unit order: bevy_brp_0.20.0-rc1 35.8 GiB (12:46 PDT), bevy_brp 19.1 GiB (12:59 PDT), `/home/natepiano/rust/tool-based-ui-frame-time/target` 3.2 GiB (15:40 PDT), `/home/natepiano/rust/tool-based-ui-demo/target` 0.7 GiB (15:44 PDT).

**Files:**
- `scripts/lint/sweep.py` — use stamp, target last use, target-first floor order, per-target floor lines, orphans per build tree; the header's "The disk floor" and "Last use" paragraphs describe the rule.
- `scripts/lint/test_sweep.py` — tests for target order, a fresh stamp outweighing older units, a shortfall exhausting the oldest target before the next, dry-run lines, stamping under a held lock, no stamp on a dry run or a named target, orphan-only floor lines, a failed removal leaving its target out, and the removal line spanning oldest to newest unit.
- `config/lint.conf` — the floor comment states the shortfall and target-first rule.

**Binds later work:** the re-measure reads the per-target lines `lint sweep: the floor took <gib> from <target>, last used <when>` from the disk-floor.service journal; they include orphan bytes and exclude groups whose removal failed, so each is GiB actually taken. The removal line's `last used <oldest> to <newest>` range spans the oldest and newest `last_used` among the chosen groups, not the first and last chosen. natedev's floor is 300 GiB (`sweep_free_floor_gib.natedev=300`), live on `build-followups` and `~/.claude` main by 15:47 PDT 2026-10-06.

**Gotchas:**
- A no-op cargo build writes nothing under `target/`, and relatime refreshes atimes at most once a day, so only the stamp records a step that compiled nothing.
- The stamp lags a lint step by up to 5 minutes (`invoke.sh`'s sweep rate limit at line 246); that only reorders targets used within the same 5 minutes, all of which sort after every longer-idle target.
- `scripts/buildlog/parse.py` counts journal lines containing `removed`, so the per-target lines avoid the word.

**Ruled out:** a `--forecast` mode (the re-measure reads the journal instead); moving the stamp ahead of `invoke.sh`'s rate limit (the lag only reorders targets used within the same 5 minutes).

### Phase 2 — Re-measure a day after the target order went live · status: todo

#### Work Order

**Blocked by:** G1.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`. State every time in PDT.

**Goal:** show whether the floor now takes from the least used targets, and what it costs per day, against the 2026-10-06 numbers.

**Spec:** every step reads; nothing outside the As-built is written.
- T_live: the commit time of the first `~/.claude` main commit that contains the Phase 1 checkpoint (`git log --format=%H -1 --grep='^checkpoint(build-followups-cache-evict): phase 1 ' build-followups-cache-evict`): `git -C ~/.claude log --ancestry-path --reverse --format='%h %cI' <checkpoint>..main | head -1`. Window W: T_live to T_live + 24 h; baseline B: the 24 h before T_live. The user lowered the floor from 500 to 300 GiB on 2026-10-06; `sweep_free_floor_gib.natedev=300` was live on `build-followups` and `~/.claude` main by 15:47 PDT, and this branch merged `build-followups` after the Phase 1 checkpoint. Name the floor in force beside every figure, split B or W at the moment the floor changed when it falls inside one, and take the change's time from `git log -S'sweep_free_floor_gib.natedev=300' build-followups -- config/lint.conf` and the time it reached `~/.claude` main.
- From `journalctl --user -u disk-floor.service` (EDT) for B and W: timer sweeps that removed output, GiB taken, orphan GiB (the `orphaned files` line) apart, and the hours from each sweep back to the newest unit it took (median, min, max), from the `last used <oldest> to <newest>` removal line. In W that line spans oldest to newest unit; in B it named the last unit chosen, which under the old order was the newest by whole days, so label B's figure as that. Flag a sweep with a `could not remove` line as incomplete. Printed GiB are rounded.
- From W's per-target lines (timer removals): GiB taken from targets whose last use was under 1 h, 1–6 h and over 6 h before the sweep, and the five targets that lost the most. The per-target lines of a complete sweep sum to its removal and orphan lines within rounding.
- From the build log (`~/.local/state/buildlog/index.sqlite`, `?mode=ro`; schema in `scripts/buildlog/index.py`): `sum(sweep_freed_bytes)` of `step='sweep'` on natedev for B and W, reported as its own daily total beside the timer's.
- Verdict, no threshold: the share of W's timer GiB taken from targets used under 1 h before the sweep, and GiB a day in W against B, compared within each floor segment (500 or 300 GiB). If the floor removed nothing in W, say so: zero GiB, no top five, no age share, the lowest free space W reached, and that the live order stays unobserved beyond Phase 1's read-only check.

**Files:**
- `docs/plans/build-followups-cache-evict.md` — the As-built.

**Seats:** 1 writer — `impl` runs the commands and reports.

**Acceptance gate:** every command exits 0; the As-built holds the B/W table with the floor beside each figure, the per-age split and the five targets (or the quiet-window statement), and the verdict.
