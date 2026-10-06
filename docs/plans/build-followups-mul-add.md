# Fused multiply-add: fast on natedev, written at the source

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Proves that `mul_add` is slow on natedev's default x86_64 target and fast with FMA on, turns FMA on for natedev's local builds and hana's Linux CI, adds a PostToolUse hook that tells an agent to write `mul_add` the moment an edit leaves a float `a * b + c`, and measures that `suboptimal_flops` failures leave hana's clippy steps.

> **Production: build-followups** — unit `mul_add-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06 08:2x PDT, deciding the nightly review's `suboptimal_flops` item: "1. let's turn it on 2. let's not rebuild everything - just let it get rebuild organically 3. you're adding a hook for long functions, add another one for mul_add so we detect that up front and tell the agent to rewrite it also 4. measure that we have eliminated long functions and mul_adds from lint discovery after the fact once we hvae this running 5. i want that lint on - i want the optimization 6. it shouldn't be build speed that improves, it should be runtime that improves - we should dissaemble somehow using a trivial rust example to make sure that without it we get the slow version and with it we get the fast version. I think you should starty a new unit director call mul_add to take this work on".

## What exists today (checked 2026-10-06 by the plan author)

- **Every Rust workspace under `~/rust` denies clippy's `nursery` group** (15 workspaces, hana among them), which holds `suboptimal_flops`; it stays denied (user, item 5). In hana's clippy steps since 2026-10-02 it is the second most frequent failing lint (120 of 1,516 steps); 345 of its 347 diagnostics are "multiply and add expressions can be calculated more efficiently and accurately".
- **natedev's default target has no FMA.** `rustc --print cfg` on x86_64-unknown-linux-gnu shows no `target_feature="fma"`; with `-C target-cpu=x86-64-v3` or `native` it does. natedev's CPU (Ryzen 9 9950X) has FMA. Without the feature, `f32::mul_add` compiles to a call to libm's `fmaf` and blocks vectorization; with it, one `vfmadd` instruction. The Mac (aarch64) always has FMA (`fmadd`). The nightly review's quick check sits in `~/.local/state/nightly-review/2026-10-06/work/lints/fma/` (`m.rs`, `bench.rs`, `m0.s`, `m3.s`); read-only reference, never edited.
- **Where rustflags come from:**
  - Local builds on natedev: `~/.cargo/config.toml`, a read-only symlink owned by `/etc/nixos` `modules/common/development.nix`, has `[target.'cfg(target_os = "linux")'] rustflags = ["-C", "link-arg=-fuse-ld=mold"]`. hana has no `.cargo/config.toml`.
  - hana CI (Linux jobs): `.github/workflows/ci.yml` sets `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS: "-C link-arg=-fuse-ld=mold"` for every job, so one set of flags keeps one copy of each crate on the runner.
  - The nightly Rust release check: `scripts/buildlog/rust_release.py:356` sets the same variable to the same string for its clippy run.
- **Results do not change.** libm's `fmaf` and the hardware instruction are both correctly rounded, so `mul_add` gives the same bits either way; Rust never fuses a plain `a * b + c` on its own. Libraries that pick an intrinsic by `cfg(target_feature = "fma")` (glam's vector `mul_add`) do change results, so hana's tests are the check.
- **rustflags are part of every compiled unit's hash**, so each target directory rebuilds in full at its next build after the flag lands, and sccache misses once per crate (user, item 2: no forced rebuild).
- **The function-length hook (`docs/plans/build-followups-fn-length-hook.md`, `stalls-unit`)** builds the machinery this plan reuses: a Rust scanner and lint-scope reader in `scripts/hooks/fn_length_lib.py`, the hook entry for Claude and Codex payloads, and `scripts/hooks/codex_hooks.py`, which installs and trusts a Codex hook. Its Facts section holds the Codex hook API, trust hashes and payload shapes; read it before Phase 3.
- **Measured detection signal** (hana clippy logs since 2026-10-02, 291 distinct mul_add positions): 227 (78%) have a float literal (`0.5`, `2.0`, `1e-5`) as an operand of the `*`; 243 (84%) hold a float literal anywhere in the expression. The rest name only variables, fields and calls (`last_baseline * diegetic.points_to_world()`), whose float type an edit hook cannot see. Shapes seen: `a * b + c`, `c + a * b`, `a * b - c`, `c - a * b`, `x += a * b`.

## Decisions (showrunner)

- **FMA on through `-C target-cpu=x86-64-v3`, for all local x86_64 Linux builds on natedev and hana's Linux CI.** Every workspace denies `nursery`, so every one pays the slow call; natedev edits `development.nix` and the user rebuilds. x86-64-v3 (2015+ CPUs with AVX2 and FMA) over `native`, so a binary built here runs on any current x86_64 machine. Builds through nix ignore `~/.cargo/config.toml` (showrunner, 2026-10-06).
- **The hana CI change goes through the hana showrunner** (another production's repository): the env line becomes `"-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`; `rust_release.py` carries the same string (this plan).
- **Prove before turning on:** Phase 1's proof gates natedev's edit and the hana relay (user, item 6).
- **A separate hook, sharing the scanner:** `scripts/hooks/mul_add_lib.py` and `scripts/hooks/post-tool-use-mul-add.py`, registered beside the fn-length hook. It imports the scanner from `fn_length_lib` rather than copying it; Phase 3 may move shared scanning code into `fn_length_lib`'s public surface once `stalls-unit` has merged its Phase 3 (showrunner).
- **Never block code clippy passes:** the hook fires only on expressions it can prove are float (Phase 3 Spec) and mirrors clippy's scope and exemptions; hana `origin/main`, which passes clippy, must show zero hits.
- **Block, like the fn-length hook**, with the rewrite named: `<file>:<line>: write <a>.mul_add(<b>, <c>) for <expr> (clippy::suboptimal_flops)`.
- **Success threshold:** 75% fewer `suboptimal_flops` failed steps per 100 hana clippy steps and 75% less sole-cause seat time per day, as the fn-length plan sets for `too_many_lines`; the re-measure reports both lints side by side (user, item 4).

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan proves FMA's runtime gain and records a `suboptimal_flops` baseline (Phase 1), puts the flag on the nightly release check (Phase 2), adds a PostToolUse hook that blocks Claude edits leaving a float multiply-add (Phase 3), extends it to Codex seats (Phase 4), and re-measures (Phase 5). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Project started:** 2026-10-06T15:33:51+00:00
- **Stack:** Python 3.13, standard library only; Rust 1.99.0 (`rustc`, `objdump`) for the Phase 1 proof only, compiled in the scratchpad, never in a repository.
- **Layout:**
  - `scripts/hooks/mul_add_lib.py` — the detector: lint scope, float multiply-add discovery, exemptions (new, Phase 3)
  - `scripts/hooks/post-tool-use-mul-add.py` — the hook entry for Claude payloads (Phase 3) and Codex `apply_patch` payloads (Phase 4) (new)
  - `scripts/hooks/test_mul_add.py` — its tests (new, Phase 3; extended Phase 4)
  - `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py` — install and trust a second hook (Phase 4; built by `stalls-unit`)
  - `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py` — the clippy rustflags (Phase 2)
  - `settings.json` — Claude hook registration (Phase 3)
  - outside the repository: `~/.local/state/mul-add-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (Phase 4)
- **Key files:** `docs/plans/build-followups-fn-length-hook.md` (machinery, Codex facts, measurement method); `scripts/hooks/fn_length_lib.py` and `post-tool-use-fn-length.py` once merged; `scripts/hooks/post-tool-use-banned-words.py` (block JSON convention); `scripts/hooks/test_brp_launch_gate.py` (hook test convention); clippy's `clippy_lints/src/floating_point_arithmetic/mul_add.rs` for the toolchain's clippy (the shapes and exemptions to mirror); `~/.local/state/nightly-review/2026-10-06/work/lints/{cost.py,loop.py,clippy_fail_lints.py}` (read-only measurement scripts; `cost.py` and `loop.py` take the lint name).
- **Test lanes:** `scripts/hooks/`, `scripts/buildlog/` — `test_*.py` beside the scripts.
- **Build:** none in the repository.
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_mul_add.py'`; `python3 -m unittest discover -s scripts/buildlog -p 'test_rust_release.py'`, from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing). `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust in the repository.
- **Invariants:**
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced scan, an unknown payload or an internal error passes the edit with at most one `systemMessage` line (`mul_add hook error: <type>: <message>`), exit 0.
  - Tests never write the real `~/.codex`, `~/.local/state/mul-add-hook` or `~/.local/state/buildlog`, never run the real `codex` or `cargo`, and never read the live `~/rust/hana` working tree: hana source comes from `git -C ~/rust/hana archive origin/main` into the scratchpad.
  - Files `stalls-unit` holds (`fn_length_lib.py`, `post-tool-use-fn-length.py`, `codex_hooks.py`, their tests, `settings.json`) are edited here only after the stalls phase that holds them has merged (gates G2, G3).
  - `/etc/nixos` and `~/rust/hana` are never edited by this unit: natedev makes the nixos edit, the hana showrunner the CI edit.
  - Python is typed throughout with no `Any` and no file-level type ignores.
  - `~/.claude` main is the live configuration; each merged phase reaches it at once.
  - Times carry their zone: this plan states PDT; natedev's journal is EDT; buildlog stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 | natedev's `~/.cargo/config.toml` carries `-C target-cpu=x86-64-v3` (user rebuild) and hana `origin/main` `ci.yml` carries it | natedev tells the unit both are live |
| G2 | Phase 3 | `stalls-unit` fn-length Claude hook phase merged | natedev sends its merge hash |
| G3 | Phase 4 | `stalls-unit` fn-length Codex phase merged and installed | natedev sends its merge hash |
| G4 | Phase 5 | 96 hours after T_codex_mul_add (Phase 4's As-built) | the clock |

## Phases

### Phase 1 — Proof: mul_add is a slow call without FMA and one fast instruction with it; baseline · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Source:** the user, item 6 (see Source).

**Goal:** this plan's As-built shows, from a trivial Rust example built two ways on natedev, the default target calling `fmaf` and x86-64-v3 emitting `vfmadd`, with measured runtimes, bit-identical results, and the flag-merge check; plus the `suboptimal_flops` baseline the re-measure compares against.

**Spec:**
- In the scratchpad (never a repository), `fma.rs` with `#[unsafe(no_mangle)]` functions, f32 and f64 each: `fused(a, b, c) = a.mul_add(b, c)`, `plain = a * b + c`, `fused_slice(xs, k, c)` and `plain_slice` over `&mut [T]`, and a dependent chain `s = black_box(s).mul_add(k, c)` (latency) beside its plain twin.
- Disassembly: `rustc -O --crate-type=lib --emit=asm` once with no `-C target-cpu` and once with `-C target-cpu=x86-64-v3`. Quote, per function, the instruction lines that do the work: the default build's `call`/`jmp` to `fmaf`/`fma` and its scalar loop; x86-64-v3's `vfmadd…ss`/`…sd` and the `vfmadd…ps`/`…pd` vector loop. Run `rustc --print cfg` both ways and quote the `target_feature="fma"` line. On the Mac over `ssh mac` (print `rc=$?` inside the command), the same default build shows `fmadd`; quote it.
- Runtime: a `main` with `std::hint::black_box` and `Instant`, built `-O` both ways: slice of 4,096 elements × 5,000 repeats and a 20,000,000-step chain, per function and type; best of 7 runs, ns per element. Table: function × {default, x86-64-v3}. Also `fused` results compared bit for bit between the two builds over 1,000,000 random inputs (fixed seed, a small xorshift; no crates): all equal.
- Flag merge: in a scratch crate with `.cargo/config.toml` holding `[target.x86_64-unknown-linux-gnu] rustflags = ["-C", "target-cpu=x86-64-v3"]` beside the user-level mold table, `cargo build -v` (scratch `CARGO_TARGET_DIR`) shows rustc receiving both `-C link-arg=-fuse-ld=mold` and `-C target-cpu=x86-64-v3`; then with `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"` set, rustc receives each flag once. Quote both rustc lines' flags. Delete the crate after.
- Baseline, from `~/.local/state/nightly-review/2026-10-06/work/lints/`, in the background with `set -o pipefail`: `python3 cost.py 4 suboptimal_flops`, `python3 loop.py 4 suboptimal_flops`, `python3 clippy_fail_lints.py 4`, and the totals query `select count(*), sum(status!=0), min(started_at) from steps where repo='hana' and step='clippy' and started_at>=datetime('now','-4 days')` on `~/.local/state/buildlog/index.sqlite` opened `?mode=ro`. Record their `sha256sum`s. Derive: `suboptimal_flops` failed steps per 100 hana clippy steps; sole-cause failed steps per 100; sole-cause seat time per day = (`loop.py` failed-call wall + repair-gap sum) / span days. Also the share of its diagnostics that are multiply-add.

**Files:**
- `docs/plans/build-followups-mul-add.md` — this phase's As-built only.

**Seats:** 1 writer — `impl` runs the commands and reports; no code or test lane.

**Acceptance gate:**
- Every command exits 0; the As-built quotes the instruction lines, the runtime table, the bit comparison, both rustc flag lines, the script hashes and the three baseline numbers.
- The proof holds: the default build calls `fmaf`/`fma`, x86-64-v3 has no such call and emits `vfmadd`, `fused_slice` and `fused` chain are faster under x86-64-v3 than under the default, and the bits match. If any part fails, the checkpoint says so and stops the plan; natedev takes it to the user.

### Phase 2 — The nightly release check builds with FMA like CI · status: todo

#### Work Order

**Blocked by:** G1.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Goal:** `rust_release.py`'s clippy run uses CI's new rustflags string, and a hana binary built after G1 shows the gain in real code.

**Spec:**
- `scripts/buildlog/rust_release.py:356`: the env value becomes `"-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`, byte-identical to hana `origin/main` `ci.yml` (read it with `git -C ~/rust/hana show origin/main:.github/workflows/ci.yml`); `test_rust_release.py:272` expects the new string.
- Real-code check (unit director, read-only): find a hana binary built after G1 cleared (a `target/debug/hana` or test binary under any `~/worktrees` or `~/rust` hana checkout, by mtime after G1's time) and one built before it; `objdump -d --no-show-raw-insn` each and count `call` instructions to `fmaf`/`fma` and `vfmadd` instructions. Report both counts for both binaries and their paths and mtimes. Never run cargo in another session's worktree.

**Files:**
- `scripts/buildlog/rust_release.py` — the env string.
- `scripts/buildlog/test_rust_release.py` — the expected string.

**Seats:** 1 writer + 1 tester (`impl` the script and the objdump check; `test` the test line).

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_rust_release.py'` green; basedpyright 0/0 on both files; the objdump counts show `fmaf` calls before G1 and none after, with `vfmadd` after.

### Phase 3 — Claude edits that leave a float multiply-add are told to write mul_add · status: todo

#### Work Order

**Blocked by:** G2.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Source:** the user, item 3 (see Source).

**Goal:** after an `Edit`, `MultiEdit` or `Write` to a `.rs` file in a package whose Cargo lints enable `suboptimal_flops`, the agent is told, for each float multiply-add the detector can prove, the line and the `mul_add` rewrite, and never for code clippy passes.

**Spec:**
- `mul_add_lib.py` (typed; frozen dataclasses), reusing `fn_length_lib`'s scanner for comments, strings, char literals, attributes and function bodies:
  - Scope: as `fn_length_lib.lint_scope`, for lint `suboptimal_flops` in group `nursery`. Exemptions: `allow`/`expect` (and `cfg_attr` forms) naming `clippy::suboptimal_flops` or `clippy::nursery`, on the item or any enclosing impl, trait, mod or file.
  - Shapes: those clippy's `mul_add.rs` flags at the installed toolchain's clippy (read it and list them in the As-built), at least `a * b + c`, `c + a * b`, `a * b - c`, `c - a * b`, `x += a * b`, `x -= a * b`, with Rust precedence: a `*` operand ends at a lower-precedence operator, a comma, a `;`, or its enclosing delimiter. Skip const items, `const fn` bodies and whatever clippy skips.
  - Float proof: an operand of the `*` is a float literal (`1.0`, `0.5_f32`, `2f64`, `1e-5`). clippy passes a glam vector times a literal (`v * 0.5 + w`); the main-corpus control finds those, and the rule excludes what it finds. Add a further proof (a binding the same function declares `: f32`/`: f64`, an `as f32`/`as f64` cast) only while the main-corpus control below stays at zero.
  - Each finding: `line`, the source text of `a`, `b`, `c` and the expression, and the rewrite `a.mul_add(b, c)` (`-c` for subtraction forms, `(-a).mul_add(b, c)` for `c - a * b`; parenthesize a non-trivial `a`).
- `post-tool-use-mul-add.py`: payload handling, block JSON and error handling as `post-tool-use-fn-length.py`; reason `<path>:<line>: write <rewrite> for <expr> (clippy::suboptimal_flops)` per finding, joined by `; `, then ` The edit was applied.`; every block appends to `MUL_ADD_HOOK_STATE` or `~/.local/state/mul-add-hook/blocks.jsonl` with `"agent": "claude"`.
- `settings.json`: one handler appended to the `Edit|MultiEdit|Write` group after the fn-length handler.
- Speed: within the fn-length hook's budget, measured the same way; Claude Code runs the group's hooks in parallel, so report whether it ends before basedpyright's.

**Files:** `scripts/hooks/mul_add_lib.py`, `scripts/hooks/post-tool-use-mul-add.py`, `scripts/hooks/test_mul_add.py` (new); `settings.json`; `scripts/hooks/fn_length_lib.py` only to expose its scanner, if needed.

**Seats:** 1 writer + 1 tester. `test` writes `test_mul_add.py` from this Spec alone: each shape flags with a literal; integer `i * 2 + j` prints nothing; exemptions on fn, impl, mod and file; a `.py` path, malformed stdin and a non-nursery crate print nothing; the exact reason text and one log line.

**Acceptance gate:**
- Tests green; basedpyright 0/0 on each changed file; `python3 -m json.tool settings.json` succeeds.
- Main-corpus control (unit director): extract `git -C ~/rust/hana archive origin/main` into the scratchpad and run the detector over every `.rs` file: **zero** non-exempt findings (main passes clippy with the lint denied). Each finding is a false positive to remove before the checkpoint; report how many the literal rule first produced and what removed them.
- Recall: over the 291 historical positions (extract them from the hana clippy step logs since 2026-10-02 as the plan author did: each `multiply and add expressions` diagnostic's `-->` line and snippet), run the detector on each snippet inside a stub function; report the fraction flagged (the literal rule's ceiling is 78%).
- Live smoke after the merge reaches `~/.claude` main, from a Claude session started after it: `Write` `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }` into a scratchpad crate denying `nursery`; the reason appears and `blocks.jsonl` gains an `"agent": "claude"` line. Record T_claude_mul_add in PDT.

### Phase 4 — Codex seats get the same block after `apply_patch` · status: todo

#### Work Order

**Blocked by:** G3.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Goal:** a Codex `apply_patch` that leaves a float multiply-add reads the same reason, on natedev and the Mac.

**Spec:** `post-tool-use-mul-add.py` accepts `apply_patch` payloads exactly as the fn-length hook does (reuse its patch-path parsing; `"agent": "codex"`). `codex_hooks.py` installs and trusts this hook's handler as a second `apply_patch` group by the same rules it uses for the fn-length handler (append a new group, never renumber existing groups; trust by `config/batchWrite`); `check` reports both. Command string byte-identical to the `settings.json` entry.

**Files:** `scripts/hooks/post-tool-use-mul-add.py`, `scripts/hooks/test_mul_add.py`, `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`.

**Seats:** 1 writer + 1 tester, split as the fn-length plan's Phase 3.

**Acceptance gate:** tests green; basedpyright 0/0; install then `check` on natedev (`dangerouslyDisableSandbox`) and on the Mac over `ssh mac` (print `rc=$?` inside) both report both hooks trusted, with existing groups unchanged; `codex exec` smoke in a scratchpad crate shows the reason and an `"agent": "codex"` log line. Record T_codex_mul_add in PDT.

### Phase 5 — Re-measure: long functions and multiply-adds out of hana's clippy failures · status: todo

#### Work Order

**Blocked by:** G4.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Goal:** one table, `too_many_lines` and `suboptimal_flops` side by side, before and after both hooks, with a verdict per lint (user, item 4).

**Spec:** confirm Phase 1's script hashes (a mismatch stops the phase); run Phase 1's commands for `suboptimal_flops` and the fn-length plan's Phase 4 commands for `too_many_lines`, in the same window, between T_codex_mul_add + 96 h and + 100 h; a window that holds hours before either lint's T_codex is reported as such. Derive the three numbers per lint; compare against Phase 1 here and the fn-length plan's Phase 1. Success per lint: failed steps per 100 ≤ 25% of its baseline, sole-cause seat time per day ≤ 25%, and the control: each hook's `blocks.jsonl` holds at least one `claude` and one `codex` line inside the window (natedev and the Mac). Residuals for `suboptimal_flops`: per diagnostic in the window, whether a block for that file precedes it, or none does, split by whether the expression holds a float literal (the detector's reach). Write the table and verdicts into the As-built and send natedev the verdict lines.

**Files:** `docs/plans/build-followups-mul-add.md` — the As-built.

**Seats:** 1 writer — `impl` runs the commands and reports.

**Acceptance gate:** all hashes match, every command exits 0, and the As-built states each success condition per lint as met or not, with its number.
