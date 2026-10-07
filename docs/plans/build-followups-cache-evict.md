# cache-evict: the disk floor takes from the least used build cache first

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; approved by the showrunner 2026-10-06 (Phase 2 `--forecast` dropped, the re-measure renumbered Phase 2); the showrunner added the memory gate as Phase 2 at 16:33 PDT, so the re-measure is Phase 3.** Below its free-space floor, the disk floor sweep removes only the shortfall, but today it spreads that across every idle target by compile age, so each active unit loses part of its working set. This plan makes it take from the least recently used target first, records each target's last build so a test rerun counts as use, names in the journal whose cache went, then makes the build memory gate hold a step until the memory it will need is free, and re-measures the floor a day later.

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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan changes the disk floor's eviction order in `scripts/lint/sweep.py` (Phase 1), makes the build memory gate (`scripts/lint/memory_gate.sh`) admit a step only when its expected peak fits (Phase 2), and re-measures the floor a day after Phase 1 is live (Phase 3). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict` on branch `build-followups-cache-evict` (unit `cache-evict-unit` of production `build-followups`). Name both in every Work Order.
- **Project started:** 2026-10-06T22:31:00+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/lint/sweep.py` — budget sweep, doc-index prune, disk floor, floor alerts; header docstring holds the policy and its measurements
  - `scripts/lint/test_sweep.py` — its tests; `FloorTests` (helpers `base()`, `hold()`, `cargo_target()`, `unit()`) covers the floor
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory`, sourced by `scripts/lint/invoke.sh` (`run_once`) and the BRP launch hook `scripts/hooks/pre-tool-use-brp-launch-gate.sh`; its rule is documented in `docs/as-built/build-memory-admission.md` "The memory gate"
  - `config/lint.conf` — `sweep_free_floor_gib.natedev` (300 GiB since the user lowered it from 500 on 2026-10-06) and its comment
  - outside the repository: `~/.local/state/lint-sweep/` (floor lock, `floor.json`), `journalctl --user -u disk-floor.service` (EDT), `~/.local/state/buildlog/index.sqlite` (UTC; read only, `?mode=ro`)
- **Key files:** `scripts/lint/sweep.py` (`Group`, `group_roots`, `scan_roots`, `choose`, `shrink`, `hold_floor`, `sweep_workspace`, `main`, header paragraphs "The disk floor" and "Last use"); `scripts/lint/invoke.sh:229–248` (`sweep_after_step`), `:355–369` (`invoke_sweep`); `scripts/buildlog/parse.py:49–52` (the lines it counts); `docs/as-built/build-memory-admission.md` "Disk-floor alerts" (as-built to amend at run end).
- **Test lanes:** `scripts/lint/` — `test_*.py` beside the scripts.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` from the worktree root. Merge tests (production): `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`.
- **Lint:** `basedpyright` on each changed Python file under `scripts/lint/` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing).
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
| G1 | Phase 3 | 24 h after Phase 1 reaches `~/.claude` main (T_live, its As-built) | the clock |

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

### Phase 2 — The memory gate holds a build until the memory it will need is free · status: done

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`. State every time in PDT.

**Source (showrunner, 2026-10-06 16:33 PDT):** earlyoom killed 5 builds between 15:31 and 16:29 PDT, the user's important test among them: hana builds in the tool-based-ui demo, widget (x2) and frame-time worktrees, and the sccache server at 16:09. `scripts/lint/memory_gate.sh` starts a build whenever `MemAvailable` is at least 12 GiB and reserves nothing, so several hana nextest steps each see 12 GiB free, all start, and each grows to 7–25 GB. Admit a step only when `MemAvailable`, less the growth still expected from steps already running, covers this step's expected peak; expected peaks from build-log history (repo + step, a high percentile of `peak_mem_bytes`), with a fallback when there is no history. Keep the 15-min limit, make the wait visible in the step's output, and replay today's kill windows.

**Measured (unit director, 16:40 PDT):** at the kills (UTC 22:31:12, 23:02:27, 23:09:36, 23:09:54, 23:29:21) 9, 7, 7, 6 and 2 steps were running, nearly all `hana` `nextest` from `verify.sh`, each peaking at 7–23 GiB (`steps.peak_mem_bytes`); every one had started with `mem_wait_s` 0–31. Over the build log's history on natedev, `hana nextest` (5,366 runs) peaks p50 10.2 GiB, p90 20.3, p95 24.6; `hana clippy` p90 7.6, `hana mend` 8.0, `hana check` 3.1, `cargo-liner nextest` 6.5. `MemTotal` is 60.5 GiB; earlyoom sends SIGTERM at 5% available. `peak_mem_bytes` is the scope's `memory.peak`, page cache included, which `MemAvailable` already counts as free: in the 105 `hana nextest` runs with no other `builds.slice` step beside them, the slice's `builds_anon_bytes` (60 s samples) peaked at a median 31% of the step's `peak_mem_bytes`, p90 61%, max 105%; `hana clippy` p90 65% (17 runs), `hana mend` 58% (22). A live `widget-enhancements` mend scope at 16:55 PDT held 1.0 GiB anon and 3.6 GiB file. So `need` and running steps' use are process memory (`anon`), not `memory.peak`.

**Goal:** a build step starts only when the memory it is expected to reach fits beside the growth still expected from the steps already running, so a burst of large test steps runs in turn instead of together and earlyoom stops killing them.

**Spec:**
- **The rule.** All sizes are process memory: `memory.stat` `anon`, which page reclaim cannot take back. For a step that compiles (`buildlog_step_compiles`, unchanged):
  - `need` — its expected anon peak, the first of these that applies, each the 90th percentile (`nearest_rank` rule: sorted index `(90*n+99)//100-1`) over this host and the same repo and step in the last 14 days, with at least 5 values:
    1. the anon peaks the gate measured itself (`admission/anon_peaks.jsonl`, below);
    2. the build log's `peak_mem_bytes` × `ANON_SHARE` = 0.65, the measured p90 anon share above (state the measurement beside the constant);
    3. otherwise the fallback, 12 GiB.

    Repo and step are derived exactly as the build log derives them: `parse.step_name(argv)` and the folder name of `record.git_facts(record.git_directory(argv, cwd))["repo_path"]` (imports from `scripts/buildlog/`, read only). The index is `store.root() / "index.sqlite"`, opened `?mode=ro`; a missing or unreadable index or history file, or no repo, skips that source.
  - `promised` — the sum, over live reservations, of `max(0, need_i - anon_i)`, where `anon_i` is the `anon` line of `memory.stat` in that step's scope cgroup; 0 when the step has no scope or the cgroup is gone, so an unscoped step counts its whole `need`.
  - `reserve` — earlyoom's line: 5% of `MemTotal`, or 0 when the meminfo has no `MemTotal` line.
  - Admit when `MemAvailable >= need + promised + reserve`. With no live reservation, admit at `MemAvailable >= min(need + reserve, 12 GiB)`: a step alone never waits for more than today's gate asked, since no running step will free anything.
- **Measuring a step.** `BUILDLOG_SCOPE_SH` (`invoke.sh`) writes its scope's cgroup directory to `"$0.cgroup"` as its first action after the marker, and reuses that path for the `memory.peak` read at its end; `record.py` never reads the sidecar. While an admitted, scoped step runs, `run_once` keeps one background sampler that reads the scope's `memory.stat` `anon` every second with shell builtins and keeps the maximum beside the reservation. On release the sampler stops, and a step that has a measured maximum appends one line to `admission/anon_peaks.jsonl`: host, repo, step, worktree, the anon peak in bytes, the step's exit status and its end time (UTC ISO). Release deletes the reservation, the maximum and the `.cgroup` sidecar.
- **The ledger.** One file per admitted step under `store.root() / "admission"` (so every test that moves `BUILDLOG_DIR` or `HOME` gets its own), holding the step's pid, that pid's start time (`/proc/<pid>/stat` field 22), `need`, repo, step, worktree, admission time and the scope sidecar's path once known. The check, the pruning and the write happen under one exclusive `fcntl.flock` on `admission/lock`, so two gates cannot both admit against the same free memory. A reservation whose pid is gone, or whose pid's start time differs, is deleted when read. A step admitted at the 15-min limit writes its reservation too.
- **Who holds it.** `buildlog_wait_for_memory [ARGV...]` takes the step's argv; the pid recorded is the calling shell's `$BASHPID`, passed explicitly. It sets `BUILDLOG_MEM_OUTCOME` and `BUILDLOG_MEM_WAIT_S` as now, plus `BUILDLOG_MEM_RESERVATION` (the reservation's path, empty when none). `buildlog_release_memory` deletes that file and clears the variable; `run_once` calls it once the step returns, on every path (terminal, piped, sandbox failure). Called with no argv (the BRP launch hook, unchanged), the gate uses the fallback `need` and writes no reservation: the launch's build runs outside the hook's process.
- **The split.** A new `scripts/lint/memory_admit.py` owns `need`, the ledger and the decision; `memory_gate.sh` keeps the poll loop, the 5 s interval and 900 s limit (`BUILDLOG_MEM_POLL_S`, `BUILDLOG_MEM_WAIT_LIMIT_S`), the build-hold marks and the outcome names (`Granted`, `TimedOut`, `MeminfoUnavailable`). The decision is one pure function over plain values (`MemAvailable`, `MemTotal` or none, `need`, and each live reservation's `need` and `anon`) returning a tagged admit or hold that carries the threshold, `promised` and `reserve`; the replay calls the same function. Python runs through `"$HOME/.claude/scripts/lib/py"`, as `record.py` does. A step admitted at once costs one Python start; name that cost in the header comment.
- **The output** (stderr, which `verify.sh` folds into the step's log):
  - On the first hold, one line, keeping today's prefix and clause so existing readers still match: `waiting for memory since HH:MM PDT: the machine has X.X GiB free; a build starts at Y.Y (<repo> <step> needs N.N GiB, <the 90th percentile of K measured runs | 0.65 of the 90th percentile peak of K runs | no history, so the fallback>; P.P GiB still promised to M running steps: <repo> <step> in <worktree>, …; R.R GiB kept for earlyoom)`. Omit the promised clause when M is 0 and the earlyoom clause when R is 0.
  - Every 60 s while held: `still waiting for memory (N min): the machine has X.X GiB free; a build starts at Y.Y`. Never repeat the `waiting for memory since ` prefix.
  - On admission after a hold: `memory free after N min S s: starting`.
  - At the limit, today's line unchanged: `memory wait limit reached after 15 min; starting anyway`.
- **Tests** (`scripts/lint/test_memory_admit.py`, fake meminfo, fake build-log index, fake cgroup tree, temporary ledger; never the real ones): the decision for alone, beside running steps, with a running step's `anon` counted, and with no `MemTotal`; `need` from measured peaks, falling to the build log at 4 measured values and using it at 5, the share applied, a stale row outside 14 days, another repo or host, and no index or history file; the sampler's maximum reaching `anon_peaks.jsonl` on release, and nothing appended for an unscoped step; the scope string writing the `.cgroup` sidecar (`test_invoke_scope.py`); two concurrent admissions against memory for one admit only one; a dead or reused pid's reservation is ignored and deleted; a step admitted at the limit holds a reservation; release deletes it; the gate's lines in order with no repeated prefix; no reservation without argv. The existing gate tests in `scripts/buildlog/test_record.py`, `scripts/build_hold/test_build_hold.py` and `scripts/delegate/test_verify_*.py` keep passing unchanged.
- **The replay** (a scratch script under the session folder, never committed, calling the pure decision): for every step that compiled and arrived (`started_at - mem_wait_s`) in the 15 minutes before each of 22:31:12, 23:02:27, 23:09:36 and 23:29:21 UTC, in arrival order, re-deciding each held step at every later sample: `need` = tier 2 above, from rows that started before 2026-10-06 22:00 UTC (no measured anon peaks exist yet); `MemAvailable` = `MemTotal` − `mem_used_bytes` of the nearest `samples` row at or before the moment decided; `promised` = the admitted, still-running steps' `need` minus that sample's `builds_anon_bytes`, floored at 0. A held step stays out of the running set until admitted, then runs for its actual duration from that moment; the 15-min limit admits it regardless. Report per window, in PDT: arrival, worktree, repo and step, `need`, free, promised, admitted or held, the wait it would have had, and its actual peak and exit status; then the steps held, their total and longest wait (the cost), and which of the steps earlyoom actually killed would have been held. The free memory replayed is what the machine actually had, including steps the gate would have held, so the waits are an upper estimate; state that and each other approximation beside the table.

**Files:**
- `scripts/lint/memory_admit.py` — new: expected peak, ledger, decision, the CLI the gate calls.
- `scripts/lint/memory_gate.sh` — the loop calls it; the output lines; `BUILDLOG_MEM_RESERVATION`; `buildlog_release_memory`; the header states the rule.
- `scripts/lint/invoke.sh` — `BUILDLOG_SCOPE_SH` writes the `.cgroup` sidecar; `run_once` passes the step's argv to the gate, runs the sampler, and releases the reservation on every path.
- `scripts/lint/test_invoke_scope.py` — the sidecar holds the scope's cgroup directory.
- `scripts/lint/test_memory_admit.py` — new: the tests above.

**Seats:** 1 writer — `impl` writes the code and tests, then runs the replay and reports its table.

**Acceptance gate:** `test_memory_admit.py`, `test_sweep.py`, `test_invoke_scope.py`, and the `scripts/buildlog`, `scripts/build_hold` and `scripts/delegate` suites named above pass; basedpyright reports `0 errors, 0 warnings, 0 notes` for `memory_admit.py` and `test_memory_admit.py`; the replay table reports, per kill window, which steps the gate would have held.

### Phase 3 — Re-measure a day after the target order went live · status: todo

#### Work Order

**Blocked by:** G1.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`. State every time in PDT.

**Goal:** show whether the floor now takes from the least used targets, and what it costs per day, against the 2026-10-06 numbers.

**Spec:** every step reads; nothing outside the As-built is written.
- T_live: the commit time of the first `~/.claude` main commit that contains the Phase 1 checkpoint (`git log --format=%H -1 --grep='^checkpoint(build-followups-cache-evict): phase 1 ' build-followups-cache-evict`): `git -C ~/.claude log --ancestry-path --reverse --format='%h %cI' <checkpoint>..main | head -1`. Window W: T_live to T_live + 24 h; baseline B: the 24 h before T_live. The user lowered the floor from 500 to 300 GiB on 2026-10-06; `sweep_free_floor_gib.natedev=300` was live on `build-followups` and `~/.claude` main by 15:47 PDT, and this branch merged `build-followups` after the Phase 1 checkpoint. Name the floor in force beside every figure, split B or W at the moment the floor changed when it falls inside one, and take the change's time from `git log -S'sweep_free_floor_gib.natedev=300' build-followups -- config/lint.conf` and the time it reached `~/.claude` main.
- From `journalctl --user -u disk-floor.service` (EDT) for B and W: timer sweeps that removed output, GiB taken, orphan GiB (the `orphaned files` line) apart, and the hours from each sweep back to the newest unit it took (median, min, max), from the `last used <oldest> to <newest>` removal line. In W that line spans oldest to newest unit; in B it named the last unit chosen, which under the old order was the newest by whole days, so label B's figure as that. Flag a sweep with a `could not remove` line as incomplete. Printed GiB are rounded.
- From W's per-target lines (timer removals): GiB taken from targets whose last use was under 1 h, 1–6 h and over 6 h before the sweep, and the five targets that lost the most. The per-target lines of a complete sweep sum to its removal and orphan lines within rounding.
- From the build log (`~/.local/state/buildlog/index.sqlite`, `?mode=ro`; schema in `scripts/buildlog/index.py`): `sum(sweep_freed_bytes)` of `step='sweep'` on natedev for B and W, reported as its own daily total beside the timer's.
- Phase 2's memory gate reaches `~/.claude` main inside W; name that time beside the verdict, since holding builds in turn can change how fast targets grow.
- Verdict, no threshold: the share of W's timer GiB taken from targets used under 1 h before the sweep, and GiB a day in W against B, compared within each floor segment (500 or 300 GiB). If the floor removed nothing in W, say so: zero GiB, no top five, no age share, the lowest free space W reached, and that the live order stays unobserved beyond Phase 1's read-only check.

**Files:**
- `docs/plans/build-followups-cache-evict.md` — the As-built.

**Seats:** 1 writer — `impl` runs the commands and reports.

**Acceptance gate:** every command exits 0; the As-built holds the B/W table with the floor beside each figure, the per-age split and the five targets (or the quiet-window statement), and the verdict.
