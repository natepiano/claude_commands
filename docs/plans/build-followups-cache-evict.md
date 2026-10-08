# cache-evict follow-up 1: scratch cargo target folders come under the disk floor

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; plan accepted by the showrunner 2026-10-08 05:0x PDT.** The disk floor finds a cargo target only by `.rustc_info.json`, so a scratch target built without that file is invisible to it and to the disk measurement: it is never taken and the alert reports it as growth outside build caches. This plan makes both recognise any folder cargo built into, and makes the floor take idle scratch targets before any managed target.

> **As-built disposition: amend** — `docs/as-built/build-memory-admission.md`, sections "Disk floor: what it removes" and "Disk-floor alerts"

> **Production: build-followups** — unit `cache-evict-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-08, from the showrunner (natedev), relaying the user. The user, about 05:00 PDT, when the showrunner said it would add a check for scratch target folders to the floor: "Yes. Scratch builds need to watch that as well".

The showrunner's measurement that morning: 172 GiB of cargo output sat in scratch target folders: `/tmp/claude/organon_ci_target` (22 GiB, grew 2.4 to 19 GiB in 8 minutes) and, in one session's scratchpad under `/tmp/claude-1000`, `pbeam_target` (140 GiB) and `pfloor_target` (9.7 GiB). They were CI-form builds with their own `CARGO_TARGET_DIR`. The floor evicted managed targets to hold 300 GiB free, and the `disk_floor` alert could only say that something outside build caches grew.

What the showrunner asked for, in behaviour:

1. The floor sweep finds scratch cargo target folders under `/tmp/claude` and the session scratchpads under `/tmp/claude-1000`, counts them, and when free space is short takes idle ones before any managed target; a folder whose cargo lock a build holds is never touched.
2. A build into a scratch target folder holds the floor itself, so it cannot fill the disk between two floor runs.
3. The `disk_floor` alert names a scratch target folder as build output, not as unexplained growth.

## What exists today (measured 2026-10-08 05:00–05:10 PDT by the unit director)

- **The floor already walks `/tmp`.** `FLOOR_ROOTS = ("~/rust", "~/.local/state", "/tmp")`; `target_dirs()` walks each root 8 levels deep and takes every directory that holds `.rustc_info.json`. At 04:52 PDT the journal shows `the floor took 10.7 GiB from …/b83cfc96-…/scratchpad/pfloor_target` and 4.6 GiB from `~/rust/cargo-liner/target`: the 15.3 GiB of that sweep. Today it finds 23 targets under `/tmp`, every one under `/tmp/claude` or `/tmp/claude-1000`.
- **A target without `.rustc_info.json` is invisible.** `pbeam_target` and `organon_ci_target` appear in no floor line all day, and the 04:52 alert lists `/tmp/claude/organon_ci_target` (largest child `…/debug`) under "Directories outside build caches that grew most". `scripts/buildlog/disk.py:128` uses the same single test.
- **Cargo leaves a second marker, and during a first build it is the only one.** Into a folder cargo creates, a build writes `CACHEDIR.TAG` and `debug/` at once, and `.rustc_info.json` only when the first build ends, or never: on 2026-10-08 the new rule found 76 targets where the old found 41, and 31 of the added ones were finished scratch builds with the tag alone. A target in a long first build is exactly the folder the alert listed. Into a folder that already exists, cargo writes `.rustc_info.json` early and no tag. With `CARGO_CACHE_RUSTC_INFO=0` it never writes `.rustc_info.json`, so a folder made first and built that way has neither marker; nothing on this machine sets that variable. The tag's second line is `# This file is a cache directory tag created by cargo.`
- **That tag alone is not proof of a target.** `~/.cargo/registry/CACHEDIR.TAG` and `~/.cargo/git/CACHEDIR.TAG` carry the identical text, and they hold no build tree. Other tools write `CACHEDIR.TAG` with the same `Signature:` line and their own comment.
- **A scratch target is ordered like any other.** `hold_floor()` sorts by the target's last use, so a 140 GiB scratch folder used ten minutes ago outlives a managed target idle for hours.

## Decisions (unit director)

- **One rule for "cargo built into this folder".** `.rustc_info.json`, or cargo's own `CACHEDIR.TAG` line with a build tree beside it. Both the floor and the disk measurement use it. The build tree is required with the tag because cargo tags its registry and git cache with the same text; treating one of those as a target would remove downloaded sources as orphaned files.
- **Scratch means under `/tmp`.** Every target the floor finds under `/tmp` today is a session scratchpad or a delegate scratch crate, so one root states the rule; `/tmp/claude` and `/tmp/claude-1000` need no separate entries.
- **Scratch first, then the existing order.** Below the floor, idle scratch targets lose output before any managed target, least recently used scratch target first. Orphaned files, which no build unit owns and nobody rebuilds, still go first from every idle target, as before. A target a build holds is never touched, scratch or not, as now.
- **Nothing is built for item 2** (agreed by the showrunner). The 2-minute timer already runs the floor for every build; the growth measured, about 2 GiB a minute, cannot cross 300 GiB of headroom between two runs.
- **Item 3 follows from the rule.** Once a scratch target counts as a build cache, its growth is cache growth in the floor's alert and the disk measurement no longer lists it outside build caches. The journal's per-target line already names each target the floor takes from.
- **`scripts/buildlog/disk.py` is the followups-unit's file.** The showrunner cleared this unit to make the one-line change there (2026-10-08); the checkpoint notice names it under `also touches`.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan widens how the disk floor in `scripts/lint/sweep.py` recognises a cargo target and puts scratch targets first in its removal order. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict` on branch `build-followups-cache-evict` (unit `cache-evict-unit` of production `build-followups`). Name both in every Work Order.
- **Project started:** 2026-10-08T12:10:00+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/lint/sweep.py` — budget sweep, doc-index prune, disk floor, floor alerts; header docstring holds the policy
  - `scripts/lint/test_sweep.py` — its tests; `FloorTests` (helpers `base()`, `hold()`, `cargo_target()`, `unit()`) covers the floor
  - `scripts/buildlog/disk.py` — the disk measurement the floor's alert reads; imports `sweep`
  - `scripts/buildlog/test_disk.py` — its tests
- **Key files:** `scripts/lint/sweep.py` (`RUSTC_INFO` at line 192, `build_trees` 347, `RemovalOrder` and `unit_age` near 602, `target_dirs` 801, `hold_floor` 1076, header paragraph "The disk floor" near line 54); `scripts/buildlog/disk.py:119–153` (`outside_directory_bytes`); `scripts/lint/test_sweep.py:278` (`cargo_target`), `:288` (`FloorTests`); `scripts/buildlog/test_disk.py:198` (the test that excludes targets).
- **Test lanes:** `scripts/lint/` and `scripts/buildlog/` — `test_*.py` beside the scripts.
- **Build:** none.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`.
- **Lint:** `basedpyright` on each changed Python file passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has; the status says nothing). Run it once after the last edit.
- **Style:** none — not Rust.
- **Invariants:**
  - Never remove output from a target whose cargo locks a build holds; the floor keeps taking every idle target's locks for its whole scan and removal, as now.
  - Never run cargo in another session's worktree. Tests build fake targets in temporary directories and never touch a real target, `~/.local/state/lint-sweep`, `~/.cargo` or the build log.
  - The floor removes no more than the shortfall plus the last group's overshoot, as now.
  - CI targets (`CI_TARGETS`, `--target-dir`) and the workspace budget sweep behave exactly as before.
  - No new sweep output line contains `removed` (`scripts/buildlog/parse.py` counts those).
  - `/etc/nixos` is the showrunner's; this plan needs no change there.
  - Python is typed throughout with no `Any` and no file-level type ignores.
  - Saved run output stays small: tests write only inside their temporary directory.

## Phases

### Phase 1 — Follow-up 1: scratch target folders come under the disk floor  · status: done

#### Work Order

**Goal:** A folder cargo built into counts as a build cache for the floor and for the disk measurement even without `.rustc_info.json`, and below the floor an idle target under `/tmp` loses output before any managed target.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-cache-evict`, branch `build-followups-cache-evict`.

1. **One rule for a cargo target** — `scripts/lint/sweep.py`, beside `RUSTC_INFO`:

   ```python
   CACHE_TAG = "CACHEDIR.TAG"
   CARGO_TAG_LINE = "# This file is a cache directory tag created by cargo."

   def is_cargo_target(directory: str) -> bool:
       """Whether cargo built into directory."""
   ```

   - True when `<directory>/.rustc_info.json` is a file, exactly as `target_dirs` tests today.
   - Otherwise True only when both hold: `<directory>/CACHEDIR.TAG` is a file with `CARGO_TAG_LINE` as one of its lines, and `build_trees(directory)` is not empty. Read at most the first 512 bytes of the tag; a tag that cannot be read or decoded is not cargo's.
   - Why both: cargo writes the identical tag into `$CARGO_HOME/registry` and `$CARGO_HOME/git`, which hold no build tree, and other tools write `CACHEDIR.TAG` with their own comment line. A folder the floor takes for a target has its unowned files removed as orphans, so a wrong yes deletes data.
   - Cost, named: one more `stat` for each directory the walk visits that has no `.rustc_info.json`; the tag is read and `build_trees` runs only where a tag exists.
   - `target_dirs()` calls `is_cargo_target(directory)` in place of its `os.path.isfile(... RUSTC_INFO)` test. Nothing else in it changes.

2. **Scratch targets go first** — `scripts/lint/sweep.py`:

   ```python
   # Targets under these roots are scratch builds: below the floor they lose output before any other target.
   SCRATCH_ROOTS = ("/tmp",)

   def is_scratch_target(root: str, scratch_roots: Sequence[str]) -> bool:
       """Whether the target directory root lies under one of scratch_roots."""
   ```

   - Compare real paths, so a symlinked root is judged by where it points; a root equal to a scratch root counts as under it.
   - `hold_floor(floor, dry_run, roots=FLOOR_ROOTS, lock=FLOOR_LOCK, scratch_roots=SCRATCH_ROOTS)` gains the last parameter so tests can name their own scratch root.
   - Its removal order becomes: scratch before managed, then the target's last use, then `unit_age`, as one `RemovalOrder` key: `(0.0 if scratch else 1.0, target_uses[target], *unit_age(group, ordered_at))`. Decide each idle target's scratch flag once, before sorting.
   - A target a build holds stays in `busy` and is untouched, as now. The printed lines do not change: `the floor took <GiB> from <target>, last used <when>` already names a scratch target by its path.
   - The workspace budget sweep (`sweep()`) is not touched.

3. **The header docstring** — `scripts/lint/sweep.py`, paragraph "The disk floor". Replace the sentence that begins "Targets under FLOOR_ROOTS (those holding .rustc_info.json)" so the paragraph says, in this order: a target is a directory under `FLOOR_ROOTS` holding `.rustc_info.json`, or cargo's `CACHEDIR.TAG` beside a build tree (a folder cargo creates carries the tag from its first moment, and `.rustc_info.json` may come only when the first build ends, or never; cargo's registry carries the same tag and has no build tree); a folder made before a build with `CARGO_CACHE_RUSTC_INFO=0` has neither and stays unseen; orphaned files go first from every idle target, as before; then idle targets under `SCRATCH_ROOTS` lose output; then the least recently used target, then by build-unit use within each target. Add one dated line of evidence: on 2026-10-08 the tag rule found 76 targets where `.rustc_info.json` alone found 41. Keep the rest of the paragraph byte for byte.

4. **The disk measurement** — `scripts/buildlog/disk.py:128`: `if sweep.is_cargo_target(directory): continue`. No other change in that file.

5. **Tests.** Each is named for the behaviour it pins.
   - `scripts/lint/test_sweep.py`, with a helper that builds a target holding cargo's tag and a `debug/.fingerprint` build tree and no `.rustc_info.json`:
     - `target_dirs` finds such a target;
     - `target_dirs` does not find a directory with cargo's tag and no build tree (the shape of cargo's registry), and its files survive a floor run;
     - `target_dirs` does not find a directory whose `CACHEDIR.TAG` has the `Signature:` line and another tool's comment, even with a `debug/.fingerprint` beside it;
     - below the floor, a scratch target used a minute ago loses output before a managed target idle for hours, and with a shortfall smaller than the scratch target the managed target loses nothing;
     - a scratch target whose cargo lock is held is left alone and the managed target loses output;
     - a tag-only target below the floor loses output and its path appears in a `the floor took` line;
     - among managed targets the existing order still holds (the existing test `test_below_the_floor_the_least_recently_used_target_goes` passes unchanged, with the helper `hold()` passing a scratch root that holds none of its targets if the temporary directory lies under `/tmp`).
   - `scripts/buildlog/test_disk.py`:
     - a tag-only target is left out of `outside_build_caches`, as the `.rustc_info.json` target is at line 198;
     - a directory with another tool's `CACHEDIR.TAG` is still counted.

**Files:**
- `scripts/lint/sweep.py` — `CACHE_TAG`, `CARGO_TAG_LINE`, `SCRATCH_ROOTS`, `is_cargo_target`, `is_scratch_target`; `target_dirs` and `hold_floor` use them; header paragraph
- `scripts/buildlog/disk.py` — line 128 calls `sweep.is_cargo_target`
- `scripts/lint/test_sweep.py` — the floor and target-finding tests above
- `scripts/buildlog/test_disk.py` — the two measurement tests above

**Seats:** 1 writer + 1 tester — the code and its tests are separate files.
- `impl` — `scripts/lint/sweep.py`, `scripts/buildlog/disk.py`; no hub file
- `test` — `scripts/lint/test_sweep.py`, `scripts/buildlog/test_disk.py`, written from the Spec's signatures and constants

**Constraints from prior phases:** none in this plan. From the earlier run, now in `docs/as-built/build-memory-admission.md` "Disk floor: what it removes": `choose()` and `shrink()` take the order as a `RemovalOrder` argument, and `hold_floor()` passes a lambda built from `target_uses` and `unit_age`; a target's last use is the newer of its `.lint-sweep-used` stamp and its units' last uses.

**Acceptance gate:** both test commands in Delegation Context green, with the new tests present and passing; `basedpyright scripts/lint/sweep.py scripts/lint/test_sweep.py scripts/buildlog/disk.py scripts/buildlog/test_disk.py` ending `0 errors, 0 warnings, 0 notes`. The unit director then lists the targets the floor finds on this machine before and after the change, without taking a lock or removing anything, and reads every folder the new rule adds.
