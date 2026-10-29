# Frozen copies: one helper makes every build copy

> **Production: builds** — unit `verify-unit`; production doc `docs/plans/builds-production.md`
> **Status:** approved by the user 2026-10-29 (via builds). Phase 1 takes the build hold after 00:00 EDT tonight, with resources-unit's case D. Unit-director survey of the path format closes 23:00 EDT; answers land here before Phase 3.

One helper, `scripts/copies/copies.py` (verify-unit), makes every frozen copy we build in: gate, review, merge and land copies on natedev, and the Mac test mirrors. It names each copy `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, gives it its own git, fills its target by the measured best option, and registers it with the cleanup that already exists.

Facts read 2026-10-29 on natedev. "unverified" marks a claim not checked.

## Today

| Copy-maker | Where | What it copies | Target handling | Cleanup | Owner |
| --- | --- | --- | --- | --- | --- |
| Gate copies, by hand (memory `gate-copy-needs-own-git`, `shared-module-full-gate`) | `/tmp/claude/{unit,delegate}/<run>/phases/<id>/…`, `/tmp/claude/scratch/gate-*`, scratchpads | worktree + own `clone --shared` .git, or `git archive` export | own empty target, cold build (`scratch/gate-bs-p4-bsp4-8ddhva/wt/target` = 9.9 GiB now) | `remove_run_scratch.py:1-28` at run end (`end_session.sh:95`); `tmp_sweep.py` idle rule; floor sweep | none — each unit by hand |
| Review / merge / land copies, by hand ("54b/code", "p34-merge", "land60", scratchpad mergeclone) | `/tmp/claude/delegate/fc206247…/phases/54b/code`, `/tmp/claude/p34-merge-aaa8ebeb9d-809cc931e7`, scratchpads | `git clone` / `clone --shared` of a hana worktree | own empty target: 54b/code reached 17 GB (9.6 incremental, 6.7 deps); the floor took it, 3.1 MiB now | as above; p34-merge sits loose in `/tmp/claude` (317 MiB) | none — by hand |
| Phase clones, mktemp folders, other scratchpad clones (W2 rows) | `/tmp/claude/<name>-XXXX`, scratchpads | clones of hana | own empty target | `tmp_sweep.py` idle rule | none — by hand |
| `merge_gate.py --nightly` | `merge_gate.py:1205-1209` | `clone --shared --no-checkout` of ~/.claude | none (Python, no cargo) | TemporaryDirectory | shutdown (natedev) |
| `waiting.py scratch-test` | `waiting.py:280-306` | `git worktree add --detach` of the unit branch + merge | own empty target if the test builds | `worktree remove --force` | unit-board (natedev) |
| `style_sync.py lint` | `style_sync.py:339-365`, `lint_target` | `git worktree add --detach` under `$TMPDIR/style-sync-lint-*` | **shared** per-repo target `<common dir>/style-sync-target`, symlinked, one `flock` at a time | own sweep + `scratch.hold` | style (natedev) |
| `rust_release.py` | `buildlog/rust_release.py:333-359` | `clone --local --no-checkout` of ~/rust/hana | own empty target | deleted on every outcome | report-unit (`scripts/buildlog/`) |
| Mac test mirror | `offload.py:845-846` (path), `offload.py:994-1031` (copy) | `rsync -rlpc --delete` of the worktree, no `.git`, no `target/` | one target per repository: `~/.local/state/mac-test/mirror/<repo>/target`, kept across runs | removed after a run only below `free_floor_gib=100` (`offload.py:1080-1094`) | verify-unit |
| Mac demo clone | `mac_run.sh:18,82` | `git clone ~/rust/hana ~/rust/hana_catalyst_mac` on the Mac | own target, kept | none | verify-unit |

Not copies for this helper: unit worktrees (`add_unit.py:821`, `resume_unit.py:176`), `berth_fix/launch.sh:128`, test fixtures (`cli_fixture.py:125`), `merge_gate.py:775,806` test temp roots, `hana_shot` temp folders, `commands/mend_fix.md:24` (by-hand rsync of external projects; out of scope).

Lifetime tools that exist: `scripts/lib/scratch.py` (tasks `run:`, `phase:`, `gate:`, `row:`, `until:`; root `/tmp/claude/scratch`, names `<task>-<word>-<6 chars>`; resources-unit), `tmp_sweep.py` (hourly, idle 4 h / 7 days), `sweep.py` floor (`FLOOR_ROOTS` includes `/tmp`, `sweep.py:216`; `source_git_dir` maps a clone to hana, `sweep.py:1788-1800`). W2: no script makes the hana clones; nothing sets `CARGO_TARGET_DIR`.

## Decision: seed vs shared build-dir

**Numbers we have.**
- /tmp clones took 249.0 of 541.9 compile-h this week (46%); about 239 h hana (W2 §1).
- First-build cold-h per week, in-scope kinds: gate/merge 5.7, phase 13.4, other scratchpad 9.2, scratch.py 7.4; mktemp 61.6; style-sync 6.4. Repeats after eviction: 145.5 cold-h; 704 of 862 cold repeats were at the same commit (W2 §1).
- ext4, no reflink: a seed is a full copy, 30-60 GiB for a full hana target (W2 §2). Copy time: unverified (W2 did not time it).
- `/` has 293 GiB free of 1.8 TiB (`df -h /`, 2026-10-29): already 7 GiB under the 300 GiB floor. Every GiB a copy adds, the floor takes from another target.
- Lock: "cargo 1.98.1 holds flock() on .cargo-lock, .cargo-build-lock and .cargo-artifact-lock for the whole build" (`sweep.py:123-126`, probed 2026-09-15). That predicts builds on one shared build-dir run one at a time. Installed cargo is now **1.99.0**, not 1.98; cargo's man pages do not cover build-dir locking. Two builds on one build-dir: untested.
- Precedent: `style_sync.py` already shares one target per repo under one flock, and still paid 6.4 first-build cold-h over 141 checkouts: a fresh checkout's source times postdate the fingerprints, so workspace crates rebuild (`style_sync.py:339-341` docstring).

**Numbers still missing.** (1) Build time of one commit cold vs seeded, with and without `incremental/`, and with source times kept vs a fresh checkout. (2) Copy time and GiB added per seed. (3) Shared build-dir: crates compiled by a second clone's build, lock wait with two builds at once, and whether clone 1 rebuilds after clone 2 (units thrash). Phase 1 measures all three in one session.

**Rule** (applied to the Phase 1 numbers; A = cold build of commit C in a fresh copy, today's method):
1. **Shared build-dir** wins when: the second clone's build compiles under 50% of A's crates, the lock wait with two builds at once is under 10% of build wall time (resources-unit's thresholds, W2 §4), and clone 1's rebuild after clone 2 compiles at most 5 crates.
2. Else **seed** wins when the best seed variant's copy s + build s is at most 50% of A's build s, and its added GiB, less the GiB a cold copy grows to, is at most 5 GiB per copy. Keep `incremental/` only if it cuts build s by 25% or more against the seed without it and still meets the GiB limit.
3. Else **neither**: the helper makes copies with an empty target (today's cost). Naming, cleanup and in-use marking still land; resources-unit's Phase 5 (sccache for workspace crates) carries the speed.
4. Both 1 and 2 pass: shared build-dir (no copy, least disk).

Expected (unverified): rule 1 fails on the lock (`sweep.py:123`), and seeding wins only without `incremental/` and only with source times kept; a full-copy seed with `incremental/` cannot fit under the floor (Disk cost). Hardlink seeding (`cp -al`) is ruled out: cargo writes outputs in place, which would change the source target (W2 §3).

## Naming

- Path: `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, e.g. `/tmp/claude/copies/hana-lfo/54b-gate`. Purpose is one word: `gate`, `review`, `merge`, `land`, `before`, `after`, `test`. A second copy of one phase and purpose gets `-2`, `-3`.
- Names come only from `roster.unit_labels_for` / `roster.unit_labels` (`scripts/message/roster.py`). Session names are short (`hana`, `lfo`, `builds`, `resources`; read from `~/.claude/sessions/*.json`), never `-unit`, slugs or ids.
- **Status of `unit_labels`: not landed.** It is uncommitted work in `/home/natepiano/worktrees/claude-natedev-builds-report/scripts/message/roster.py:241-272` (report-unit Phase 3, `builds-report.md:142`, status todo). It is not on any branch (`git log --all -S unit_labels` is empty) and not in `~/.claude/scripts/message/roster.py`. Phase 3 below waits for it.
- The caller's session: `build_hold.owning_session()` (`build_hold.py:291`: the unit director first, then `CLAUDE_CODE_SESSION_ID`), the same lookup Mac claims use. A Codex worker so resolves to its director.
- Two gaps go to report-unit, in `roster.py`, never in the helper: (a) the join `<showrunner>-<unit>` as one function, which drops a unit name's leading showrunner name (`cargo-liner-style` + `cargo-liner-style-types` must not double); (b) a missing part (`—`) is dropped, so a showrunner alone names `builds/…`.

## Cleanup

Each copy is known to cleanup at birth. The helper does not add a cleaner.
- **Record:** the helper makes the folder through `scratch.py` with a task: `phase:<unit>/<phase>` for gate, review and land copies (ended by `merge_checkpoint.py:970`), `gate:<id>` for a gate the caller ends, `run:<run id>` for run copies (ended by `end_session.sh:95`), else `until:` 24 h. The record also holds the provenance: source path, source commit, copy commit, target mode, seed source.
- **Layout support (resources-unit, `scratch.py`):** a copies root `/tmp/claude/copies` with the two-level name; `tmp_sweep.py` treats each `<pair>/<copy>` as an entry and removes an empty `<pair>/`; the scratch guard hook allows the root.
- **Floor:** a copy's target in use is busy to the floor sweep (resources-unit Phase 3, under repair). An idle copy's target is taken least recently used first, as now.
- **Shared build-dir (if chosen):** one per repository at `/tmp/claude/copies/build-dirs/<repo>`, held with `scratch.hold` like `style_sync.py lint_target`, under the hana budget (`sweep_budget_gib.hana=96`, `config/lint.conf:66-67`).
- **Mac:** see Mac mirrors.
- **Git:** the copy's own `clone --shared` git dir borrows objects from the source; a copy outliving a `git gc` of the source can lose objects. Copies are short-lived; the 7-day idle rule bounds it.

## Disk cost

Per copy, hana, natedev:

| Option | GiB at creation | GiB after one build | Source |
| --- | --- | --- | --- |
| Today (empty target) | ~0.3 (p34-merge 317 MiB) | 10-17 (gate-bs-p4 9.9; 54b/code 17), up to 30-60 | du, W2 §2 |
| Seed with `incremental/` | 30-60 | 30-60 + changed units | W2 §2 |
| Seed without `incremental/` | about 17-34 (54b ratio: incremental was 9.6 of 17) | + changed units | unverified, Phase 1 |
| Shared build-dir | 0 | registry units once per repo (deps 6.7 GiB in 54b, upper bound) + workspace units per copy | unverified, Phase 1 |

Busy day: in-scope clones run about 78 a week (12 gate/merge + 31 phase + 22 other scratchpad + 13 scratch.py; W2 §1), about 11 a day; mktemp (291) and style-sync (141) add short-lived ones. Now: 23 cargo targets under `/tmp/claude` (find, depth 7) and about 56 GiB in `/tmp/claude/{scratch,delegate,unit}` (du; some folders unreadable). Assume 6 copies live at once (unverified). Against 293 GiB free and a 300 GiB floor:
- today: 6 × 10-17 = 60-100 GiB, all taken back by the floor from other targets;
- seed with incremental: 6 × 30-60 = 180-360 GiB: does not fit;
- seed without incremental: 6 × 17-34 = 100-200 GiB: fits only if it replaces the cold growth, hence the 5 GiB net limit in the rule;
- shared build-dir: about 7 GiB + per-copy workspace units.

## Mac mirrors

- Today: one mirror per repository, `~/.local/state/mac-test/mirror/<repo>` (`offload.py:845-846`), one claim per repository, target kept until the Mac is under 100 GiB free.
- Change: `~/.local/state/mac-test/copies/<showrunner>-<unit>/<repo>`, names from the same roster call. My call: the mirror spans phases, so its last part is the repo, not `<phase>-<purpose>`; a one-line change if the user wants otherwise.
- Seed: a new mirror's target is an APFS clone (`cp -c -R`) of the newest mirror of that repo: near zero time and disk until written. The Mac disk is APFS: unverified.
- Claims stay per repository for hana (one at a time). Per-unit mirrors are what the next item "two cargo-liner runs on the Mac at once" (`docs/plans/natedev-verify-next.md:3-9`) needs.
- Cleanup: under the floor, remove the least recently used mirror target first, then mirrors whose unit is not in the roster.
- Every path match moves with it: `mirror_selector_script` (`offload.py:1126-1160`), the activity check (`mac_test.py:1507-1521`), `mac_idle.py`, `docs/as-built/mac-test-offload.md:86-91`.
- `~/rust/hana_catalyst_mac` stays: the user demos from it (`commands/showrunner/produce.md:788-793`).

## Delegation Context

- **Project:** ~/.claude scripts — one helper for frozen build copies.
- **Stack:** Python 3 (basedpyright strict, zero warnings), bash, git, cargo 1.99.0.
- **Layout:** `scripts/copies/` (new), `scripts/mac_test/`, `scripts/production/mac_run.sh`.
- **Key files:** `scripts/lib/scratch.py` (lifetime, resources-unit); `scripts/message/roster.py` (names, report-unit); `scripts/build_hold/build_hold.py:291` (owning session); `scripts/lint/sweep.py:187` (`LOCK_NAMES`, reuse for the seed lock); `scripts/mac_test/offload.py`.
- **Test lanes:** `scripts/copies/test_copies.py`; `scripts/mac_test/test_offload*.py`.
- **Build / Test:** `HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <tree> --tests-only` on a tree merged with origin/builds; `basedpyright` on changed files and every caller module.
- **Invariants:** a copy has its own git dir, never a copied `.git` pointer (memory `gate-copy-needs-own-git`); a copy's `origin` is the source's origin URL, not the source path (`docs/plans/sdlc-phases.md:252`); one name source (roster); no copy outside `/tmp/claude/copies` once a caller moves.

## Phases

### Phase 1 — Measure seed vs shared build-dir on one hana commit  · status: todo

#### Work Order

**Goal:** the numbers the Decision rule needs, and the rule applied.

**Spec:** One session under `/build:hold` (natedev quiet), hana at one commit C, then C+1 with one workspace-crate edit. Cases: A cold fresh copy; B seed with `incremental/`; C seed without it; each of B and C twice, sources fresh-checked-out vs copied with `cp -a` then own git (`clone --shared --no-checkout`, move `.git`, `git reset -q`, `git checkout --detach C`); D shared `CARGO_BUILD_BUILD_DIR`: clone 1 then clone 2 in sequence, then both at once, then clone 1 again. Hold the source's `.cargo-lock`, `.cargo-build-lock`, `.cargo-artifact-lock` during each seed copy. Record per case: copy wall s, GiB added (`du`), build wall s, crates compiled, lock wait s. Run with sccache as today. resources-unit's Phase 4 lock trial runs as case D, in this session (builds, 2026-10-29). builds sets the build-hold time. E: resources-unit's Phase 5 compile-cache trial, two hana clone builds one after the other, on the same commit C, run last so cases A-D see today's cache (builds, 2026-10-29). resources-unit has its setup ready before 23:00 EDT and agrees it with verify-unit, including what cache state E needs at its start.

**Files:** none changed; results to the builds LOG and this plan's Decision section.

**Acceptance gate:** a table of cases A-E with every metric; the rule's verdict written into the Decision section; cargo version named.

Hold: 00:05-03:05 EDT (builds, 2026-10-29), taken with `/build:hold` with its reason and until time, released the moment the last case ends. Every trial folder (source worktree, seeds, clones, shared build-dir) lives under `~/copies-trial/`, on the same file system as `/tmp` but outside the floor sweep's roots (`FLOOR_ROOTS` in `scripts/lint/sweep.py`), so an idle seed target is never trimmed between two builds of a case. The sweep has no hold of its own today, and it does not read `scratch.py` holds. Each case's copies are deleted as soon as its numbers are recorded; peak extra disk is one case at a time, about 55 GiB (case D: one shared build-dir plus two clones' final output), with E removing clone 1's target before clone 2 builds. Each build records `/proc/loadavg` and `/proc/pressure/cpu` sampled every 10 s (start, mean, max), so a quiet-machine number is told apart from a busy one.

Hold length: 3 h. Count: about 17 hana builds — A 2, B 4, C 4, D 5 (one pair at once), E 2 — plus 4 seed copies. Basis: a 603-crate hana build took 928 s on a busy natedev today (build log, 10:35 EDT); a quiet machine is assumed at about 12 min cold and 2 min for C+1. If every seed compiles fully it is about 2 h 40 min; if the seeds work it is about 1 h 45 min. The Bevy dylib change (13:50 EDT) is in C, so it is in every case. If the hold ends before E, E moves to the next hold; A-D never split.

### Phase 2 — Prerequisites from other units  · status: todo

#### Work Order

**Goal:** names and cleanup support exist where they live.

**Spec:** report-unit lands `unit_labels` / `unit_labels_for` (its Phase 3) plus the join function and dropped-part rule (Naming). resources-unit adds the copies root to `scratch.py` (two-level name, provenance fields), `tmp_sweep.py` and the scratch guard; its Phase 3 in-use marking covers `/tmp/claude/copies`. If Phase 1 picks the shared build-dir, resources-unit owns its budget and hold.

**Files:** owned by report-unit and resources-unit; verify-unit sends the requests.

**Acceptance gate:** both merged on builds.

Rough duration: half a day each, in parallel with Phase 1.

### Phase 3 — The helper  · status: todo

#### Work Order

**Goal:** `copies.py make|list|remove` makes a named, registered copy with the chosen target mode.

**Spec:** `copies.py make --source <worktree> --commit <sha> --phase <id> --purpose <word> [--task <task>]` prints the path. Steps: name from roster via `owning_session`; folder through `scratch.py`; own git (`clone --shared --no-checkout`, origin = source's origin URL, check `git rev-parse --absolute-git-dir` names the copy); target per Phase 1 verdict (seed: `cp -a` under the three source locks, skip `incremental/` unless the rule kept it; shared: set `CARGO_BUILD_BUILD_DIR` in the copy's `.cargo/config.toml`; neither: empty). `list` shows copies, task, source, size. `remove` ends one copy through scratch.py. Python API `make_copy(...)` for callers.

**Files:** `scripts/copies/copies.py`, `scripts/copies/test_copies.py`, `docs/as-built/frozen-copies.md`.

**Constraints from prior phases:** Phase 1 verdict; Phase 2 names and scratch layout.

**Acceptance gate:** tests green (fake roster, fake source repo, the linked-worktree `.git` case); live: one hana gate copy made, built, and removed by `merge_checkpoint.py` ending its phase task.

Rough duration: one day.

### Phase 4 — Move the copy-makers to the helper  · status: todo

#### Work Order

**Goal:** no build copy is made outside the helper.

**Spec:** Hand-made copies: replace the recipe in memory notes `gate-copy-needs-own-git`, `shared-module-full-gate`, `merge-before-gate` and the verify rows of `commands/unit/direct.md` with `copies.py make`. Owners switch their own calls (one notice each): `waiting.py scratch-test` (unit-board, natedev), `merge_gate.py --nightly` (shutdown, natedev), `rust_release.py` (report-unit), `style_sync.py lint` (style, natedev; keeps its shared target if Phase 1 picks shared). mktemp folders: the scratch guard names `copies.py` in its refusal (resources-unit).

**Files:** `commands/unit/direct.md` (verify rows); others by their owners.

**Acceptance gate:** a week of the build log shows no new hana clone outside `/tmp/claude/copies` (report-unit query).

Rough duration: half a day for verify-unit; owners' changes about an hour each.

### Phase 5 — Mac mirrors per unit  · status: todo

#### Work Order

**Goal:** Mac mirrors use the copies naming and an APFS-clone seed.

**Spec:** as in Mac mirrors above. Path from the helper's name function; first run of a new mirror clones the newest same-repo mirror's target with `cp -c -R`; cleanup least recently used first, then retired units; move every path match. Check APFS first; on another file system, seed nothing.

**Files:** `scripts/mac_test/offload.py`, `scripts/mac_test/mac_test.py`, `scripts/mac_test/copy_watch.py`, `scripts/buildlog/mac_idle.py`, tests, `docs/as-built/mac-test-offload.md`.

**Acceptance gate:** offload tests green; live: two units' hana runs in turn, each in its own mirror, second mirror's first build reuses the clone (crates compiled under 50% of cold).

Rough duration: one day.

### Phase 6 — Rollout check  · status: todo

#### Work Order

**Goal:** prove the gain on real work.

**Spec:** time one real gate commit cold and through the helper (sdlc's design point); after one week, compare /tmp compile-h (W2 §1 method) and floor takes with this week.

**Files:** none; result to builds.

**Acceptance gate:** helper copy build at most 50% of cold, or the plan returns to builds with the numbers.

Rough duration: half a day, then a one-week watch.
