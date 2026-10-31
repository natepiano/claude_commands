# Frozen copies: one helper makes every build copy

> **Production: builds** — unit `verify-unit`; production doc `docs/plans/builds-production.md`
> **Status:** approved by the user 2026-10-29 (via builds). Phase 1 done: measured 2026-10-30 02:00-02:41 EDT under verify-unit's build hold; the builds showrunner set the target mode to seed on 2026-10-30 (Decision, Verdict). Unit-director survey of the path format closed 23:00 EDT; its answers are in Survey changes.

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

Lifetime tools that exist: `scripts/lib/scratch.py` (tasks `run:`, `phase:`, `gate:`, `row:`, `until:`; root `/tmp/claude/scratch`, names `<task>-<word>-<6 chars>`; resources-unit), `tmp_sweep.py` (hourly; end evidence, else 7 days idle: since `3b89f3aa`, merged as `d72d9b8f` on 2026-10-29, no 4 h rule for top-level `/tmp/claude` entries or unit/delegate run folders; without end evidence an entry goes only after 7 days with no change, mtime or ctime anywhere in the tree, and no process), `sweep.py` floor (`FLOOR_ROOTS` includes `/tmp`, `sweep.py:216`; `source_git_dir` maps a clone to hana, `sweep.py:1788-1800`). W2: no script makes the hana clones; nothing sets `CARGO_TARGET_DIR`.

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

**Verdict (Phase 1, 2026-10-30).** Measured on hana `c421a1a6` with cargo 1.99.0 and sccache, 02:00-02:41 EDT under verify-unit's build hold; all cases exit 0. Table: `/tmp/claude/unit/bf5b8d4c-77ff-4f7b-9318-afb6114ad8f2/copies/results/results.tsv`.

| Case | Copy s | GiB added | Build C s / crates | Build C+1 s / crates | Lock wait s |
| --- | --- | --- | --- | --- | --- |
| A cold fresh copy | 0 | 11.70 (target) | 224.8 / 568 | 27.1 / 5 | 0 |
| B seed + `incremental/`, fresh checkout | 40.6 | 15.07 | 78.9 / 25 | 15.8 / 5 | 0 |
| B seed + `incremental/`, `cp -a` | 63.9 | 15.07 | 0.7 / 0 | 77.2 / 5 | 0 |
| C seed, no `incremental/`, fresh checkout | 27.0 | 10.29 | 78.6 / 25 | 15.2 / 5 | 0 |
| C seed, no `incremental/`, `cp -a` | 18.9 | 10.30 | 1.7 / 0 | 75.5 / 5 | 0 |
| D shared build-dir: clone 1, clone 2 | 0 | 14.62 (folder) | 256.4 / 568, then 0.7 / 0 | pair at once: 68.8 / 5 and 69.0 / 0; clone 1 again 0.6 / 0 | 68.7 of 69.0 |

Case E (resources-unit's compile-cache trial): clone builds with `CARGO_INCREMENTAL=0` got 0 workspace-crate cache hits, and mend with sccache took 132.4 s against 115.4 s without. Neither was adopted.

- Rule 1 fails: with two builds at once, one waited 68.7 s of 69.0 s on the lock (99%, limit 10%). Clone 2 (0 crates) and clone 1 again (0 crates) pass.
- Rule 2 passes: seed C with `cp -a` takes 18.9 + 1.7 = 20.6 s, 9% of A's 224.8 s, and adds 10.30 GiB against A's 11.70 GiB (-1.4 GiB net). `incremental/` cuts no build time (C+1 77.2 s against 75.5 s), so it is dropped.
- **Target mode: seed, without `incremental/`, sources copied with `cp -a`** (builds showrunner, 2026-10-30). `copies.py` has this one mode and no option; it has no shared target mode.

Risks:
1. Each live copy holds about 10.3 GiB. The helper's cleanup must end copies on time, or copies push `/` under the 300 GiB floor and the sweep trims other targets.
2. The first edit after a `cp -a` seed takes 75.5 s, against 15.2 s for a fresh checkout. The 25-crate rebuild that a fresh checkout pays at C moves to the first edit.

## Naming

- Path: `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, e.g. `/tmp/claude/copies/hana-lfo/54b-gate`. Purpose is one word: `gate`, `review`, `merge`, `land`, `before`, `after`, `test`. A second copy of one phase and purpose gets `-2`, `-3`.
- Names come only from `roster.unit_labels_for` / `roster.unit_labels` (`scripts/message/roster.py`). Session names are short (`hana`, `lfo`, `builds`, `resources`; read from `~/.claude/sessions/*.json`), never `-unit`, slugs or ids.
- **Status of `unit_labels`: landed.** report-unit Phase 3 (`e4f5e836`) added `unit_labels` / `unit_labels_for`, and its Phase 4 (`2c96f13a`) added `UnitLabels.folder_name()`. Both are on origin/builds and in `~/.claude/scripts/message/roster.py`.
- The caller's session: `build_hold.owning_session()` (`build_hold.py:291`: the unit director first, then `CLAUDE_CODE_SESSION_ID`), the same lookup Mac claims use. A Codex worker so resolves to its director.
- The join lives in `roster.py`, never in the helper: `UnitLabels.folder_name()` gives `<showrunner>-<unit>` (hana + lfo gives `hana-lfo`), drops a unit name's leading showrunner name (`cargo-liner-style` + `cargo-liner-style-types` gives `cargo-liner-style-types`), and drops a missing part (`—`), so a showrunner alone names `builds`.

## Cleanup

Each copy is known to cleanup at birth. The helper does not add a cleaner.
- **Record:** the helper makes the folder through `scratch.py` with a task: `phase:<unit>/<phase>` for gate and review copies (ended by `merge_checkpoint.py:970`), `landed:<unit>/<branch>` for `work` and land copies, `gate:<id>` for a gate the caller ends, `run:<run id>` for run copies (ended by `end_session.sh:95`), else `until:` 24 h. Record fields (resources-unit, 2026-10-29): source path, source commit, copy commit, target mode, seed source, `borrows_from`, owner unit, task. A `phase:` record also names its run dir and phase id (9 fields). A `landed:` record also keeps its land target (default `origin/main`); the end check reads it from there. The record is the `.scratch.json` file inside the copy.
- **Layout support (resources-unit, `scratch.py`):** `/tmp/claude/copies` is a kept root, like `scratch/` and `unit/`. Each `/tmp/claude/copies/<pair>/<copy>` is one entry; its record, `<copy>/.scratch.json`, is its end evidence. `tmp_sweep.py` removes an empty `<pair>/`. The scratch guard hook allows the root.
- **Busy lock:** a lock file `.claude-busy` in a copy or target folder, held with a shared `flock` by each holder for the whole run, so two runs in one copy do not wait on each other. `tmp_sweep.py`, scratch end and the floor sweep test it with a non-blocking exclusive try and skip the folder when the try fails. Holders: `verify.sh` and `merge_gate.py --checkout <copy>`, each only in a tree under `/tmp/claude/copies`, and hana's frozen builds. The lock dies with its process, so a crashed run leaves no stale mark. No `~/.claude` import is needed: hana sets it with `flock`.
- **Floor:** a copy's target in use is busy to the floor sweep (resources-unit Phase 3, under repair), and so is a folder with a locked `.claude-busy`. An idle copy's target is taken least recently used first, as now.
- **Shared build-dir (if chosen):** only if case D's verdict meets all three conditions of rule 1. One per repository, returned by a `scratch.py` function (resources-unit Phase 4; it adds no clone command), at `/tmp/claude/copies/build-dirs/<repo>`, held with `scratch.hold` like `style_sync.py lint_target`, under the hana budget (`sweep_budget_gib.hana=96`, `config/lint.conf:66-67`).
- **Mac:** see Mac mirrors.
- **Git:** the copy's own `clone --shared` git dir borrows objects from the source; a copy outliving a `git gc` of the source can lose objects. The record's `borrows_from` names the source git dir; scratch end and `tmp_sweep.py` keep a parent copy while a live record borrows from it. The 7-day idle rule bounds the rest.

## Survey changes

Survey of every unit director, sent 14:05 EDT 2026-10-29 to 45 units, closed 23:00 EDT. 45 sent, 45 answered: 42 by 19:05 EDT; two (phases, cargo-liner-style-values) whose 14:05 answers sat unread in the send.py queue until the close; and resources-unit, owner of the Lifetime and cleanup items, at 23:2x EDT ("suits, with these changes"). security-unit started after the survey. Each answer, with its unit, is in `.claude/handoff/copies-survey-answers.md` of this worktree. Most said the format suits them. These changes go into Phase 3; the unit that asked is in brackets.

**Name.**
- The phase part is a slug, `[a-z0-9-]`: `54b`, `94b`, `adhoc` for ad hoc work. A slash is refused. Where no phase exists, a free label takes its place: `sync-test`, `demo27-fastest4`, `adhoc-stallcpu-gate` [unit-board, merge-gate, messaging, fix-rounds, models]. A land copy keeps its name until its push, also when the landing moves to another phase (made for `60`, landing under `47a`); a landing that spans phases can take a free label in place of the phase [startup].
- A purpose can carry one suffix word, for copies that differ on purpose: `34-before-beeece8b`, `42-work-port` [tile-unit, berth]. `-2`, `-3` stay for a second copy made at the same time, picked with an atomic `mkdir` [style-unit, models].
- New purpose `work`: a writable clone with its own branch; edits, commits, merges and pushes are allowed; it lives until its branch lands [style-unit, lfo, inspectors, organon, trunk, cables, herdr].
- `--unit <unit>`: a showrunner makes a copy for another unit [unit-board].
- Names come from roster labels only, never from a session id [models].

**Source.**
- Any git repository: hana, `~/.claude`, cargo-liner, `/etc/nixos`, `~/rust/bevy_brp`; not only unit worktrees [bevy-upgrade, herdr, roles, report].
- `--with-diff`: copy HEAD plus the working-tree diff (tracked changes and untracked files, less ignored ones). The record says "HEAD + diff" with the diff's hash [fix-rounds, models].
- Unpushed local commits come with the copy: clone from the source path, then set origin to the source's origin URL [messaging, shutdown].
- The copy gets the source's tracking ref for its merge target (`origin/<merge branch>` and `origin/HEAD`). A stale `origin/HEAD` made the assertion guard count 116 changes that were not the phase's [trunk].
- A `~/.claude` copy without `b8ef1b37` is refused: an older tree's test deletes every session folder [models].
- The helper never writes into the source worktree (`review.sh` fails on a worktree change) [models].

**Target.**
- Mode `none` when the source has no `Cargo.toml`: no seed, no shared build-dir, no cargo lock. `merge_gate.py --checkout <copy>` then runs in the copy [roles, hierarchies, fix-rounds, screenshot, report, style-unit, models, phases].
- A seed comes only from a target built with the same `Cargo.lock` and toolchain [hana-migration]. The newest landed gate copy of the repository is the first seed choice [merge-gate].
- A before/after pair gets the same fill from the same base build [fastest].

**Lifetime and cleanup** (resources-unit, `scratch.py`; Phase 2 request; resources-unit's answer, 23:2x EDT, in each item).
- Task `landed:<unit>/<branch>`: ended when the branch lands, not at the first checkpoint [widget, lfo, startup, style-unit]. Ended when `origin/<branch>` is an ancestor of the branch's land target and the copy has no uncommitted or unpushed work; the `tmp_sweep.py` git check already keeps unpushed work. The land target is the production's Merge branch when the unit is in a production, else `origin/main`; the record keeps it. The 7-day no-change rule still applies after that.
- A queued or running `verify.sh` run marks its copy busy; no idle rule or phase end removes a busy copy [widget, fix-rounds]. A memory wait of 4 h 34 min once passed the old 4 h idle rule; that rule is gone (`3b89f3aa`), and the busy lock still guards phase end, the floor and the 7-day rule. The mark is the `.claude-busy` lock (Cleanup); `verify.sh` takes it before its memory wait, so a queued run is busy too.
- A `phase:` copy is not idle while its phase is open [organon, frame-cost]. Scratch reads the phase's open state from the run's progress recorder; the record names the run dir and phase id. An open phase keeps the copy at any age.
- A copy whose origin is a sibling copy is removed before that sibling [startup]. The record keeps `borrows_from` (the parent copy's git dir, from `clone --shared`). Scratch end and `tmp_sweep.py` skip a parent while a live record borrows from it, and remove it after the last child. This also guards against lost objects.
- Removing a `before` copy never removes its `after` copy [shutdown]. They are two separate records with no nesting; nothing to build.
- `copies.py hand-over <path> --to <unit> --task <task>` moves the cleanup owner when a phase moves [demo]. It calls `scratch.py hand-over <path> --to <unit> --task <task> [--run-dir <run dir>] [--land-target <land target>]`, which keeps the path and replaces the owner and task in one atomic record write. The land target defaults to `origin/main`. The folder name stays the maker's; the record names the owner. A rename would break child copies' `objects/info/alternates` (absolute paths into the parent's git dir) and resources-unit Phase 14's push-recovery records (keyed by the resolved checkout path), and a rename plus a record write cannot be one atomic step.
- A same-name copy at another source commit is replaced or refused, never built in [screenshot]; a same-name copy at the same commit is reused across review passes [style-unit].
- A copy whose borrowed objects were pruned by `git gc` is refused loudly; the record's `borrows_from` names the borrowed git dir [models].

**Out of scope.**
- hana's product frozen builds (`scripts/perf/build.py`, `hana-measure-*`) stay outside the helper. They set the `.claude-busy` lock with `flock` in their target, with no `~/.claude` import; the floor sweep skips it (resources-unit Phase 21) [benchmark].
- Codex work clones under `~/.cache` and delegate folders may move to `work` copies; each owner decides in Phase 4 [cables, trunk].
- A Windows VM copy place: not in this plan [research].
- Before/after profile trees that need `config` linked to `~/.claude/config` make the link themselves after make [accounts].
- Mac exact-tree gate copies for clippy and nextest go to Phase 5 [port].

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

### Phase 1 — Measure seed vs shared build-dir on one hana commit  · status: done

#### Work Order

**Goal:** the numbers the Decision rule needs, and the rule applied.

**Spec:** One session under `/build:hold` (natedev quiet), hana at one commit C, then C+1 with one workspace-crate edit. Cases: A cold fresh copy; B seed with `incremental/`; C seed without it; each of B and C twice, sources fresh-checked-out vs copied with `cp -a` then own git (`clone --shared --no-checkout`, move `.git`, `git reset -q`, `git checkout --detach C`); D shared `CARGO_BUILD_BUILD_DIR`: clone 1 then clone 2 in sequence, then both at once, then clone 1 again. Hold the source's `.cargo-lock`, `.cargo-build-lock`, `.cargo-artifact-lock` during each seed copy. Record per case: copy wall s, GiB added (`du`), build wall s, crates compiled, lock wait s. Run with sccache as today. resources-unit's Phase 4 lock trial runs as case D, in this session (builds, 2026-10-29). builds sets the build-hold time. E: resources-unit's Phase 5 compile-cache trial, two hana clone builds one after the other, on the same commit C, run last so cases A-D see today's cache (builds, 2026-10-29). resources-unit has its setup ready before 23:00 EDT and agrees it with verify-unit, including what cache state E needs at its start. Agreed 15:2x: E runs `python3 ~/.claude/scripts/agents/mesh_runs.py start --label "case E clone cache" -- bash /tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/phases/5/case_e.sh "$HANA_COMMIT" "$HOME/copies-trial/E/a" "$HOME/copies-trial/E/b"` after D (dry run passed 15:13 EDT). Clones are `git clone --shared` of ~/rust/hana, built with `verify.sh build hana` and `CARGO_INCREMENTAL=0`; one target on disk at a time; then two mend arms (no wrapper, sccache). No sccache reset; no other sccache client during E. Numbers: clone b's workspace-crate hits, each mend arm's wall, exit and diagnostics hash, sccache stats and disk before and after. E's wall time is not comparable with A-D. Estimate about 1 h (slow 2 h 24 min); if A-D end more than 1 h 55 min into the hold, the mend arms move to the next hold.

**Files:** none changed; results to the builds LOG and this plan's Decision section.

**Acceptance gate:** a table of cases A-E with every metric, each build's load, and the floor sweep's trimmed total for the hold; the rule's verdict written into the Decision section; cargo version named.

Hold: 3 h from its real start, not 00:05 by the clock (re-timed by builds, 21:5x EDT 2026-10-29). It starts on the builds showrunner's go after natedev's Secure Boot sitting ('sitting 1 restored'), or at 00:05 EDT after 'no sitting tonight'; a reboot in the sitting would kill the runs. Taken with `/build:hold` with its reason and until time, released the moment the last case ends. Every trial folder (source worktree, seeds, clones, shared build-dir) lives under `~/copies-trial/`, on the same file system as `/tmp` but outside the floor sweep's roots (`FLOOR_ROOTS` in `scripts/lint/sweep.py`), so an idle seed target is never trimmed between two builds of a case. The sweep has no hold of its own today, and it does not read `scratch.py` holds. Each case's copies are deleted as soon as its numbers are recorded; peak extra disk is one case at a time, about 55 GiB (case D: one shared build-dir plus two clones' final output), with E removing clone 1's target before clone 2 builds. Each build records `/proc/loadavg` and `/proc/pressure/cpu` sampled every 10 s (start, mean, max), so a quiet-machine number is told apart from a busy one. `~/copies-trial/` is removed on every exit path: a trap in the trial script, and a check 3 h after the real start, because the floor sweep cannot reclaim it. The 300 GiB floor stays (user rule); the results table carries the floor sweep's trimmed total for the hold window, so the morning cost is counted (builds, 2026-10-29).

Hold length: 3 h. Count: about 17 hana builds — A 2, B 4, C 4, D 5 (one pair at once), E 2 — plus 4 seed copies. Basis: a 603-crate hana build took 928 s on a busy natedev today (build log, 10:35 EDT); a quiet machine is assumed at about 12 min cold and 2 min for C+1. If every seed compiles fully it is about 2 h 40 min; if the seeds work it is about 1 h 45 min. The Bevy dylib change (13:50 EDT) is in C, so it is in every case. If the hold ends before E, E moves to the next hold; A-D never split.

### Phase 2 — Prerequisites from other units  · status: todo

#### Work Order

**Goal:** names and cleanup support exist where they live.

**Spec:** report-unit lands `unit_labels` / `unit_labels_for` (its Phase 3, `e4f5e836`, done) plus the join function and dropped-part rule (Naming). resources-unit answered 23:2x EDT 2026-10-29 ("suits, with these changes"). The builds showrunner ruled: resources-unit builds the lifetime items itself as its Phase 21, "copies root and lifetime", and no verify-unit phase builds them again; verify-unit phases only call or consume them (Phase 3). Phase 21 covers: the copies root as a kept root in `scratch.py` (two-level name, the record fields of Cleanup), `tmp_sweep.py` and the scratch guard; the `.claude-busy` lock read by `tmp_sweep.py`, scratch end and `sweep.py` (busy lock only), and taken by `merge_gate.py --checkout <copy>` (an also-touches line; natedev owns the file); tasks `landed:` and open-phase `phase:`; `borrows_from` child before parent; `scratch.py hand-over <path> --to <unit> --task <task> [--run-dir <run dir>] [--land-target <land target>]` (land target default `origin/main`), which keeps the path and replaces owner and task in one atomic record write; and entries for the record (`.scratch.json`) and `.claude-busy` in the copy's `.git/info/exclude`, so a clean ended copy is removable while a real untracked file still keeps it. Files: `scripts/lib/scratch.py`, `scripts/lint/tmp_sweep.py`, `scripts/lint/sweep.py`, the scratch guard hook and their tests. It shares no files with resources-unit's memory admission phases, so it runs beside its Phase 15; ETA 06:00 EDT 2026-10-30 (slot approved by the builds showrunner). Its Phase 3 in-use marking covers `/tmp/claude/copies`. If case D picks the shared build-dir (all three conditions of rule 1), resources-unit owns its budget and hold, and its Phase 4 gives a `scratch.py` function that returns the repository's shared build folder; Phase 4 adds no clone command. efficiency-unit changes `scripts/lib/nix_env.py` to trust a copy's `.envrc` only when the copy's copies.py/scratch.py record names a source repo under `~/rust`, `~/worktrees` or `~/.claude*`, and the copy's `.envrc` equals the source's `.envrc` blob at the copy commit (approved by builds). Today it refuses (`direnv: .envrc is blocked`), so verify.sh and style_sync lint fail in a copy. verify-unit sends efficiency the record's path and fields (source path, source commit, copy commit) when Phase 3 fixes them. Due before Phase 4. resources-unit's Phase 21 covers every Lifetime and cleanup item of Survey changes (before/after needs nothing); report-unit's share is done: `UnitLabels.folder_name()` (its Phase 4, `2c96f13a`, on origin/builds; hana + lfo gives `hana-lfo`) gives the copies folder name.

**Files:** owned by report-unit, resources-unit and efficiency-unit; verify-unit sends the requests.

**Acceptance gate:** all three merged on builds (report-unit's merged; resources-unit's is its Phase 21).

Rough duration: half a day each, in parallel with Phase 1.

### Phase 3 — The helper  · status: todo

#### Work Order

**Goal:** `copies.py make|list|remove` makes a named, registered copy with the chosen target mode.

**Spec:** `copies.py make --source <worktree> --commit <sha> --phase <id> --purpose <word> [--task <task>]` prints the path. Steps: folder name from `unit_labels_for([build_hold.owning_session()], env)[…].folder_name()` (report-unit, hana + lfo gives `hana-lfo`); folder through `scratch.py`; own git (`clone --shared --no-checkout`, origin = source's origin URL, check `git rev-parse --absolute-git-dir` names the copy); target per Phase 1 verdict (seed: `cp -a` under the three source locks, skip `incremental/` unless the rule kept it; shared: get the repository's shared build folder from the `scratch.py` function (resources-unit Phase 4) and set `CARGO_BUILD_BUILD_DIR` to it in the copy's `.cargo/config.toml`; neither: empty). `list` shows copies, task, source, size. `remove` ends one copy through scratch.py. Python API `make_copy(...)` for callers. Every item of Survey changes under Name, Source and Target. Of Lifetime and cleanup, this phase builds nothing: resources-unit Phase 21 builds it (Phase 2). This phase only calls or consumes it:
- The record gets every field of Cleanup: source path, source commit, copy commit, target mode, seed source, `borrows_from` (the source's git dir from `clone --shared`; a sibling copy's when the source is a copy), owner unit, task. A `phase:` record also gets the run dir and phase id, from the caller's run.
- `work` and land copies get task `landed:<unit>/<branch>` and the land target in the record: the production's Merge branch when the unit is in a production, else `origin/main`. A land copy keeps its name until its push.
- `copies.py hand-over <path> --to <unit> --task <task>` calls `scratch.py hand-over <path> --to <unit> --task <task> [--run-dir <run dir>] [--land-target <land target>]` (resources-unit), which keeps the path; copies.py does not rename the folder or edit the record itself. The land target defaults to `origin/main`.
- `make` writes nothing to the copy's `.git/info/exclude`: resources-unit Phase 21 adds the entries for the record (`.scratch.json`) and `.claude-busy`.
- Seed copies and the disk floor (builds ruling 2026-10-31, from resources' floor diagnosis): the seed copies files, never hard-links them, and writes the `.lint-sweep-used` stamp in the copy's target when made. `cp -a` keeps file times, so without the stamp the floor has no use record before the first build (`sweep.py:1854`) and can take the seed early; a hard-linked take frees nothing (`:1446`) but still deletes the copy (`:1526`), a cold build for no space. Resources-unit Phase 37 changes the floor order (empty idle targets first); judge against that order.
- `verify.sh`: in a tree under `/tmp/claude/copies`, take a shared `flock` on `<tree>/.claude-busy` before the memory wait, so a queued run is busy too, and hold it until the run ends.

**Files:** `scripts/copies/copies.py`, `scripts/copies/test_copies.py`, `scripts/direct/verify.sh` and a verify.sh test in `scripts/direct/`, `docs/as-built/frozen-copies.md`.

**Constraints from prior phases:** Phase 1 verdict; Phase 2 names and scratch layout; dependency: resources-unit Phase 21, "copies root and lifetime" (kept copies root, `.claude-busy` readers, `landed:`, open-phase `phase:`, `borrows_from` child before parent, `scratch.py hand-over --task`, `.scratch.json` record, `.git/info/exclude` entries), ETA 06:00 EDT 2026-10-30.

**Acceptance gate:** tests green (fake roster, fake source repo, the linked-worktree `.git` case, a seed copy that holds no hard link to its source and has a fresh `.lint-sweep-used` stamp, a `.claude-busy` lock held from before the memory wait to the run end, `hand-over` to another unit keeps the path); live: one hana gate copy made, built, and removed by `merge_checkpoint.py` ending its phase task.

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

**Spec:** as in Mac mirrors above. Path from the helper's name function; first run of a new mirror clones the newest same-repo mirror's target with `cp -c -R`; cleanup least recently used first, then retired units; move every path match. Check APFS first; on another file system, seed nothing. A Mac gate copy of an exact tree (`<phase>-gate`), reusing the unit mirror's target, for offload clippy and nextest [port].

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
