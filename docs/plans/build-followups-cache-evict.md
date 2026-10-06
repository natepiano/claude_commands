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
- **A target's last use** is the newest of its use stamp's mtime and its units' `last_used`. The stamp `.lint-sweep-used` at the target root is written by every workspace sweep, before its lock check, so a step that ran in that workspace counts even when it compiled nothing; the units' times cover builds outside `lint` that compiled something. `--target-dir` (CI) writes no stamp.
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
  - `config/lint.conf` — `sweep_free_floor_gib.natedev=500` and its comment (lines 69–74)
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
  - Saved run output stays under a few GB: read each run and delete it before the next. A disk-floor sweeper keeps 500 GiB free on `/` by removing build output, least recently used target first once Phase 1 merges.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 | 24 h after Phase 1 reaches `~/.claude` main (T_live, its As-built) | the clock |

## Phases

### Phase 1 — Below the floor, the least recently used target loses output first · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`. State every time in PDT.

**Goal:** a build that pushes `/` under the floor costs output from the least recently used idle targets, not a slice of every active unit's working set, and the journal names each target that lost output.

**Spec:**
- **Use stamp.** `USE_STAMP = ".lint-sweep-used"`. `sweep_workspace()`, when `target_dir is None`, sets the stamp's mtime to now in every root `cargo_roots()` returned (create it empty if missing), before `lock_trees`, so a skipped sweep still records the step. `--target-dir` writes none. A failure to write it is ignored (never fails the sweep).
- **Target last use.** `target_last_use(root, groups) -> float`: the newest of the stamp's mtime (0 when absent) and `max(group.last_used)` over the target's groups (0 when none).
- **Group knows its target.** `Group` gains `target_used: float = 0.0`. `hold_floor` fills it for every group of each idle target from `target_last_use` (map each group to its target through the build tree it came from; `group_roots` iterates trees, so a tree→root map built in `hold_floor` from `build_trees(root)` serves). The workspace sweep leaves it 0.
- **One order.** `choose()` sorts by `(group.target_used, -whole days since last_used, group.compiled)`. With `target_used` 0 everywhere the workspace sweep's order is unchanged.
- **Per-target lines.** `choose()` returns each chosen group with the bytes it freed. After the existing removal line, a floor sweep prints one line per target that lost output, largest first: `lint sweep: the floor took <gib> from <target>, last used <when(target_used)>`; a dry run says `would take`. Only `hold_floor` prints them; the workspace sweep's output is unchanged. Paths print in full. No line contains `removed`.
- **Docs in this unit's files.** Rewrite the header's "The disk floor" paragraph: the shortfall sets how much goes; targets go least recently used first, by the stamp and the units' times; a target a build holds is never touched; why (the 2026-10-06 numbers: 801 GiB taken by the timer that day, median 3.0 h since last use; the 60 GiB simulation). Add to "Last use": a no-op build writes nothing under `target/`, so only the stamp records it. Update `config/lint.conf`'s floor comment to the new rule.
- **Live check, read only, after the tests pass:** a scratchpad script imports this worktree's `sweep`, scans `target_dirs(FLOOR_ROOTS)` without taking locks, and prints, for 60 GiB of shortfall, the targets the new order takes from and how much, beside today's order. Record both lists in the As-built. It removes nothing.
- **Text for natedev**, sent with the checkpoint notice: the replacement header comment for `disk-floor.nix` lines 2–4 ("the least recently used output of every idle target" → least recently used target first, only the shortfall).

**Tests** (`FloorTests`):
- An idle target last used 1 h ago whose units were all compiled 3 h ago, and one last used 10 min ago holding one unit compiled 5 h ago; a one-unit shortfall removes a unit of the first and nothing of the second. (Today's order would take the second's.)
- A target whose units are old but whose stamp is fresh goes after one whose units are newer and has no stamp.
- A shortfall larger than the oldest target empties it, then takes from the next oldest only what remains.
- The per-target line names the target and its last use; `would take` on a dry run; no output line contains `removed` beyond the existing removal line.
- `sweep_workspace` writes the stamp in each root, also when a build holds a lock (the sweep is skipped); `--target-dir` writes none.
- Update `test_below_the_floor_the_least_recently_used_unit_of_any_target_goes` to the target rule (rename it); the busy-target and at-the-floor tests stay as they are.

**Files:**
- `scripts/lint/sweep.py` — stamp, target last use, order, per-target lines, header
- `scripts/lint/test_sweep.py` — the tests above
- `config/lint.conf` — floor comment

**Seats:** 1 writer.

**Acceptance gate:** `test_sweep.py` passes; basedpyright reports `0 errors, 0 warnings, 0 notes` on both Python files; the live check's two lists are in the As-built, and the new order's 60 GiB list takes from no target used in the last hour while any idle target used earlier holds output.

### Phase 2 — Re-measure a day after the target order went live · status: todo

#### Work Order

**Blocked by:** G1.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`. State every time in PDT.

**Goal:** show whether the floor now takes from the least used targets, and what it costs per day, against the 2026-10-06 numbers.

**Spec (read only):**
- Window W: T_live to T_live + 24 h; baseline B: the 24 h before T_live.
- From `journalctl --user -u disk-floor.service` (EDT) for B and W: timer sweeps that removed output, GiB taken, and the hours from each sweep back to the newest unit it took (median, min, max), parsed as this plan's "What exists today" did.
- From W's per-target lines: GiB taken from targets whose last use was under 1 h, 1–6 h and over 6 h before the sweep, and the five targets that lost the most.
- From the build log (`?mode=ro`): `sum(sweep_freed_bytes)` of `step='sweep'` on natedev for B and W.
- Verdict, no threshold: the share of W's GiB taken from targets used under 1 h before the sweep, and GiB a day in W against B.

**Files:** `docs/plans/build-followups-cache-evict.md` — the As-built.

**Seats:** 1 writer — `impl` runs the commands and reports.

**Acceptance gate:** every command exits 0; the As-built holds the B/W table, the per-age split, the five targets and the verdict.
