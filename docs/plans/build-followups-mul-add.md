# Fused multiply-add: fast on natedev, written at the source

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Proves that `mul_add` is slow on natedev's default x86_64 target and fast with FMA on, turns FMA on for natedev's local builds and hana's Linux CI, adds a PostToolUse hook that tells an agent to write `mul_add` the moment an edit leaves a float `a * b + c`, and measures that `suboptimal_flops` failures leave hana's clippy steps.

> **Production: build-followups** — unit `mul_add-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06 08:2x PDT, deciding the nightly review's `suboptimal_flops` item: "1. let's turn it on 2. let's not rebuild everything - just let it get rebuild organically 3. you're adding a hook for long functions, add another one for mul_add so we detect that up front and tell the agent to rewrite it also 4. measure that we have eliminated long functions and mul_adds from lint discovery after the fact once we hvae this running 5. i want that lint on - i want the optimization 6. it shouldn't be build speed that improves, it should be runtime that improves - we should dissaemble somehow using a trivial rust example to make sure that without it we get the slow version and with it we get the fast version. I think you should starty a new unit director call mul_add to take this work on".

## What exists today (checked 2026-10-06 by the plan author)

- **Every Rust workspace under `~/rust` denies clippy's `nursery` group** (15 workspaces, hana among them), which holds `suboptimal_flops`; it stays denied (user, item 5). In hana's clippy steps since 2026-10-02 it is the second most frequent failing lint (120 of 1,516 steps); 345 of its 347 diagnostics are "multiply and add expressions can be calculated more efficiently and accurately".
- **natedev's default target has no FMA.** `rustc --print cfg` on x86_64-unknown-linux-gnu shows no `target_feature="fma"`; with `-C target-cpu=x86-64-v3` or `native` it does. natedev's CPU (Ryzen 9 9950X) has FMA. Without the feature, `f32::mul_add` compiles to a call to Rust's built-in `fmaf` (which picks the FMA instruction at run time on a CPU that has it) and blocks vectorization; with it, one `vfmadd` instruction. The Mac (aarch64) always has FMA (`fmadd`). The nightly review's quick check sits in `~/.local/state/nightly-review/2026-10-06/work/lints/fma/` (`m.rs`, `bench.rs`, `m0.s`, `m3.s`); read-only reference, never edited.
- **Where rustflags come from:**
  - Local builds on natedev: `~/.cargo/config.toml`, a read-only symlink owned by `/etc/nixos` `modules/common/development.nix`, has `[target.'cfg(target_os = "linux")'] rustflags = ["-C", "link-arg=-fuse-ld=mold"]`. hana has no `.cargo/config.toml`.
  - hana CI (Linux jobs): `.github/workflows/ci.yml` sets `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS: "-C link-arg=-fuse-ld=mold"` for every job, so one set of flags keeps one copy of each crate on the runner.
  - The nightly Rust release check: `scripts/buildlog/rust_release.py:356` sets the same variable to the same string for its clippy run.
- **Results do not change.** Rust's built-in `fmaf` (hardware FMA when the CPU has it, else a software fallback) and the hardware instruction are both correctly rounded, so `mul_add` gives the same bits either way; Rust never fuses a plain `a * b + c` on its own. Libraries that pick an intrinsic by `cfg(target_feature = "fma")` (glam's vector `mul_add`) do change results, so hana's tests are the check.
- **rustflags are part of every compiled unit's hash**, so each target directory rebuilds in full at its next build after the flag lands, and sccache misses once per crate (user, item 2: no forced rebuild).
- **The function-length hook (`docs/as-built/build-followups-fn-length-hook.md`, `stalls-unit`)** builds the machinery this plan reuses: a Rust scanner and lint-scope reader in `scripts/hooks/fn_length_lib.py` and the hook entry for Claude payloads. Its Codex phase (the `apply_patch` entry and `scripts/hooks/codex_hooks.py`, which installs and trusts a Codex hook) moved to this plan as Phase 4 on 2026-10-06. Its Facts section holds the Codex hook API, trust hashes and payload shapes; read it before Phase 3.
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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan proves FMA's runtime gain and records a `suboptimal_flops` baseline (Phase 1), puts the flag on the nightly release check (Phase 2), adds a PostToolUse hook that blocks Claude edits leaving a float multiply-add (Phase 3), extends the fn-length hook to Codex seats (Phase 4, moved from `stalls-unit`), extends the mul_add hook to Codex seats (Phase 5), widens the detector (Phase 6), makes a Codex seat's buildlog rows carry its own thread id (Phase 7, a showrunner follow-up), makes the detector's scaling test hold under load (Phase 8, a showrunner follow-up), and re-measures (Phase 9). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Project started:** 2026-10-06T15:33:51+00:00
- **Stack:** Python 3.13, standard library only; Rust 1.99.0 (`rustc`, `objdump`) for the Phase 1 proof only, compiled in the scratchpad, never in a repository.
- **Layout:**
  - `scripts/hooks/mul_add_lib.py` — the detector: lint scope, float multiply-add discovery, exemptions (new, Phase 3)
  - `scripts/hooks/post-tool-use-mul-add.py` — the hook entry for Claude payloads (Phase 3) and Codex `apply_patch` payloads (Phase 5) (new)
  - `scripts/hooks/test_mul_add.py` — its tests (new, Phase 3; extended Phase 5)
  - `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py` — install and trust the fn-length Codex hook (new, Phase 4), then a second hook (Phase 5)
  - `scripts/hooks/post-tool-use-fn-length.py` and the `apply_patch` cases in `scripts/hooks/test_fn_length.py` — Codex payloads for the fn-length hook (Phase 4)
  - `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py` — the clippy rustflags (Phase 2)
  - `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py` — the `session` precedence for a Codex seat (Phase 7; `followups-unit`'s files, edited on the showrunner's word)
  - `settings.json` — Claude hook registration (Phase 3)
  - outside the repository: `~/.local/state/mul-add-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (Phases 4 and 5)
- **Key files:** `docs/as-built/build-followups-fn-length-hook.md` (machinery, Codex facts, measurement method); `scripts/hooks/fn_length_lib.py` and `post-tool-use-fn-length.py` once merged; `scripts/hooks/post-tool-use-banned-words.py` (block JSON convention); `scripts/hooks/test_brp_launch_gate.py` (hook test convention); clippy's `clippy_lints/src/floating_point_arithmetic/mul_add.rs` for the toolchain's clippy (the shapes and exemptions to mirror); `~/.local/state/nightly-review/2026-10-06/work/lints/{cost.py,loop.py,clippy_fail_lints.py,tml_lengths.py}` (read-only measurement scripts; `cost.py` and `loop.py` take the lint name).
- **Test lanes:** `scripts/hooks/`, `scripts/buildlog/` — `test_*.py` beside the scripts.
- **Build:** none in the repository.
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_mul_add.py'`; `python3 -m unittest discover -s scripts/buildlog -p 'test_rust_release.py'`; `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` and `-p 'test_codex_hooks.py'` (Phases 4 and 5); `python3 -m unittest discover -s scripts/buildlog -p 'test_record.py'` (Phase 7), from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing). `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust in the repository.
- **Invariants:**
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced scan, an unknown payload or an internal error passes the edit with at most one `systemMessage` line (`mul_add hook error: <type>: <message>`), exit 0.
  - Tests never write the real `~/.codex`, `~/.local/state/mul-add-hook` or `~/.local/state/buildlog`, never run the real `codex` or `cargo`, and never read the live `~/rust/hana` working tree: hana source comes from `git -C ~/rust/hana archive origin/main` into the scratchpad.
  - `fn_length_lib.py` and `settings.json` are `stalls-unit`'s: edit them only on the showrunner's word (G2 cleared the one `settings.json` handler). From Phase 4, `post-tool-use-fn-length.py`, `codex_hooks.py`, `test_codex_hooks.py` and the `apply_patch` cases in `test_fn_length.py` are this unit's (moved 2026-10-06 13:5x PDT).
  - `/etc/nixos` and `~/rust/hana` are never edited by this unit: natedev makes the nixos edit, the hana showrunner the CI edit.
  - Python is typed throughout with no `Any` and no file-level type ignores.
  - `~/.claude` main is the live configuration; each merged phase reaches it at once.
  - Times carry their zone: this plan states PDT; natedev's journal is EDT; buildlog stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 | natedev's `~/.cargo/config.toml` carries `-C target-cpu=x86-64-v3` (user rebuild) and hana `origin/main` `ci.yml` carries it | natedev tells the unit both are live |
| G2 | Phase 3 | `stalls-unit` fn-length Claude hook phase merged | natedev sends its merge hash |
| G3 | Phase 5 | this unit's Phase 4 (fn-length Codex hook) merged, then installed and smoked on both machines | natedev sends its merge hash; the install and smoke pass |
| G4 | Phase 9 | 96 hours after T_detector (Phase 6's As-built) | the clock |

## Phases

### Phase 1 — Proof: mul_add is a slow call without FMA and one fast instruction with it; baseline · status: done

#### As-built

Measured on natedev on 2026-10-06 with Rust 1.99.0, from a scratch `fma.rs` of `#[unsafe(no_mangle)]` f32 and f64 functions: `fused` (`a.mul_add(b, c)`), `plain` (`a * b + c`), `fused_slice`/`plain_slice` over `&mut [T]`, and dependent chains `s = black_box(s).mul_add(k, c)` beside a plain twin, each built `rustc -O` for the default target, `-C target-cpu=x86-64-v3` and `-C target-cpu=znver5`. `rustc --print cfg`: the default has no `target_feature="fma"`; v3 and znver5 print it, and znver5 also prints `target_feature="avx512f"`.

| f32 function (f64: `sd`/`pd`, `fma`) | Default x86-64 | x86-64-v3 | znver5 |
| --- | --- | --- | --- |
| `fused` | `jmpq *fmaf@GOTPCREL(%rip)` | `vfmadd213ss %xmm2, %xmm1, %xmm0` | — |
| `fused_slice` | `movq fmaf@GOTPCREL(%rip), %r12`; `callq *%r12`, one scalar call per element (f64: `%r14`) | `vfmadd132ps %ymm2, %ymm3, %ymm4`; tail `vfmadd132ss %xmm0, %xmm1, %xmm2` | `vfmadd132ps %zmm2, %zmm3, %zmm4` |
| `plain_slice` | `mulps %xmm2, %xmm4`; `addps %xmm3, %xmm4` (128-bit) | `vmulps (%rdi,%r9,4), %ymm2, %ymm4`; `vaddps %ymm4, %ymm3, %ymm4` | the same on `zmm` |
| `fused_chain` | `movq fmaf@GOTPCREL(%rip), %r15`; `callq *%r15` | `vfmadd132ss %xmm1, %xmm2, %xmm0` | — |

Neither v3 nor znver5 has an `fmaf`/`fma` call. The default build's `fmaf`/`fma` is Rust's built-in, not glibc's: the binary imports no `fma` symbol, and `nm` shows `compiler_builtins::math::libm_math::arch::x86::fma` linked in, whose `initializer` points a `FUNC` slot at `fmaf_with_fma` (`vfmadd213ss %xmm2,%xmm1,%xmm0`; `ret`) on a CPU with FMA, else at `fmaf_fallback`. On natedev the default build runs the FMA instruction after two indirect calls; its cost is the calls and the lost vectorization. On the Mac the default `rustc -O --crate-type=lib --emit=asm` emits `fmadd s0, s0, s1, s2`, `fmadd d0, d0, d1, d2`, and slice loops `fmla.4s v7, v3, v0[0]` / `fmla.2d v7, v3, v0[0]`.

Runtime, best of seven interleaved sets, ns per element (slice: 4,096 elements × 5,000 repeats) or per step (`black_box` chain: 20,000,000 steps); load average 92.12, 58.42, 45.52 at start and 93.39, 59.24, 45.85 at end:

| f32 / f64 | Default | x86-64-v3 | znver5 |
| --- | ---: | ---: | ---: |
| `fused_slice` | 2.805349 / 2.570618 | 0.037627 / 0.066127 | 0.028691 / 0.055911 |
| `plain_slice` | 0.066430 / 0.154128 | 0.032391 / 0.071408 | 0.030304 / 0.055825 |
| `fused_chain` | 3.055681 / 3.044925 | 3.062240 / 3.052307 | 3.063683 / 3.049501 |
| `plain_chain` | 3.516461 / 3.507783 | 3.521767 / 3.520615 | 3.522537 / 3.519772 |

`fused_slice` over default: v3 74.6× f32 / 38.9× f64, znver5 97.8× / 46.0× (1.31× and 1.18× over v3). A rerun of the same binaries at load 58.65: v3 77.4× / 41.6×, znver5 124.8× / 57.1× (f32 2.857880 → 0.036929 → 0.022896 ns; f64 2.551594 → 0.061355 → 0.044675). The register-resident dependent chain, `s = s.mul_add(k, c)` for 200,000,000 steps with `s`, `k`, `c` and the count through `black_box` once at the call, gains 2.5× for f32 and f64 (best of seven interleaved, load 71.99, ns per step). Its default loop is eight unrolled `callq *%r14` through `fmaf@GOTPCREL`; v3's is eight `vfmadd213ss %xmm2, %xmm1, %xmm0`.

| Register chain, f32 / f64 | Default | x86-64-v3 | Speedup |
| --- | ---: | ---: | ---: |
| fused | 2.7195 / 2.5597 | 1.0876 / 1.0052 | 2.5× / 2.5× |
| plain (control) | 1.5541 / 1.5784 | 1.7241 / 1.6326 | — |

Bit equality: the `fused_f32` and `fused_f64` bit patterns over 1,000,000 fixed-seed xorshift triples of each type form one 12,000,000-byte stream per build; `cmp default.bits v3.bits` and `cmp default.bits znver5.bits` exit 0, and all three streams have SHA-256 `3f7f390cafdebd9ccf91ce22c1a571eec657580f8b93d9e7942fc632f91df758`.

Flags, from `cargo build -v` in a scratch crate: a crate `.cargo/config.toml` `[target.x86_64-unknown-linux-gnu] rustflags = ["-C", "target-cpu=x86-64-v3"]` beside the user mold table gives rustc `-C target-cpu=x86-64-v3 -C link-arg=-fuse-ld=mold`. On natedev, `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS='-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3'` joins the crate's triple table and the user-level `cfg(target_os = "linux")` table, so rustc gets `-C target-cpu=x86-64-v3 -C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3 -C link-arg=-fuse-ld=mold`. CI's case (empty `CARGO_HOME`, no crate config, the environment string alone) passes `-C link-arg=-fuse-ld=mold` and `-C target-cpu=x86-64-v3` once each.

Baseline at 2026-10-06 08:39:09 PDT over hana clippy steps since 2026-10-02 09:09:00 PDT (3.979 days), from `~/.local/state/nightly-review/2026-10-06/work/lints/` (`cost.py 4 suboptimal_flops`, `loop.py 4 suboptimal_flops`, `clippy_fail_lints.py 4`) and `select count(*), sum(status!=0), min(started_at) from steps where repo='hana' and step='clippy' and started_at>=datetime('now','-4 days')` on `~/.local/state/buildlog/index.sqlite` opened `?mode=ro`. Script SHA-256: `cost.py` `c2de20870ab2de9b5438e469c565d25c48185438ab42dd77807d817655b5d4fa`; `loop.py` `a775765b96311a783c8d6af8cdc925071b89b4ac423ae8ffa75b88fe81bf9abf`; `clippy_fail_lints.py` `e178642d9bb1690562c132d824256198cc8f6e622b779b94ca76abbc75e841ce`.

| Measure | Baseline | Limit | Derivation |
| --- | --- | --- | --- |
| `suboptimal_flops` failed steps per 100 hana clippy steps | 7.916 | ≤ 1.979 | 120 / 1,516 (473 failed); 349 diagnostics |
| Sole-cause failed steps per 100 | 2.573 | — | 39 / 1,516 |
| Sole-cause seat time a day | 0.402 h | ≤ 0.100 h | 1.60 h (0.64 h failed-call wall + 0.96 h repair gaps) / 3.979 days; `loop.py` finds 36 sole-cause failed lint calls, 33 with another within 1,800 s (gap median 82 s, p75 135 s) |
| Multiply-add share of diagnostics | 347 / 349 (99.4%) | — | `error: multiply and add expressions may be calculated more efficiently and accurately` lines; `cost.py` splits 219 `src`, 130 test/example |

**Files:**
- `docs/plans/build-followups-mul-add.md` — this record; no repository code. The scratch `fma.rs`, its binaries and the flag-check crate are outside the repository.

**Binds later work:**
- The nightly release check's disassembly gate counts `fmaf`/`fma` references per function outside compiler_builtins, GOTPCREL loads included, with the default binary as the before-control.
- The re-measure compares against the unrounded baselines (7.916 and 2.573 per 100, 0.402 h a day) with the same three scripts and totals query, and passes at ≤ 1.979 per 100 and ≤ 0.100 h a day.
- Once `rust_release.py` sets the environment string on natedev, its rustc gets each flag twice: the same values, so the same code, as its mold flag already gets.

**Gotchas:**
- `black_box(s)` in a dependent chain sends `s` through the stack each step (`vmulss -4(%rsp), %xmm1, %xmm0` in v3's plain chain); that round trip, about 3 ns, sets the pace in every build and hides the call. Only a chain that keeps the value in registers shows the FMA latency.
- A whole-binary `vfmadd` count includes compiler_builtins' `fmaf_with_fma`, so it is nonzero in the default build; the count that separates builds is per function outside compiler_builtins.
- The default build calls `fmaf` through a register after a GOTPCREL load (`callq *%r12`), so a name-based call count misses it.
- On natedev the environment string plus the user config pass each target flag twice with the same values; CI's environment alone passes each once.
- `cargo build -v --manifest-path` from a parent directory does not discover the crate's `.cargo/config.toml`; rustc then gets only the mold flag.

**Ruled out:** x86-64-v4 for 512-bit vectors, because its LLVM tuning keeps 256-bit `ymm` (0 `zmm` lines against 14 for znver5); only znver5/native emits `zmm`.

### Phase 2 — The nightly release check builds with FMA like CI · status: done

#### As-built

`trial_release()`'s clippy step in `scripts/buildlog/rust_release.py:356` runs with `CARGO_INCREMENTAL=0` and `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS` set to `"-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`, byte-identical to hana `origin/init/catalyst:.github/workflows/ci.yml:77`; `origin/main` keeps the mold-only string until catalyst merges. `test_rust_release.py:272` asserts that string on the recorded clippy call's environment; the 28 tests pass, and basedpyright reports 0 errors, 0 warnings, 0 notes on both files. The flag changes the clippy build's target hash; the release clone's target is fresh each run, so no cache is lost.

The live run is deferred to the next nightly release check's clippy step, the first run with the new flags; its buildlog entry shows whether that step finished. A manual run is a full hana clippy build in a fresh clone.

Real-code check, `objdump -d -C --no-show-raw-insn` counted per function, on two debug test binaries of the same crate in the same checkout, `~/rust/tool-based-ui-trunk`, one from each side of the user `~/.cargo/config.toml` change at 2026-10-06 09:45:18 PDT (its line 84: `[target.x86_64-unknown-linux-gnu] rustflags = ["-C", "target-cpu=x86-64-v3"]`). The crate metadata hash changes with the flags, and the after binary uses VEX encodings (`vmulss`).

| Binary | mtime (PDT) | `fmaf`/`fma` refs outside compiler_builtins | `vfmadd` outside compiler_builtins |
| --- | --- | --: | --: |
| before: `target/debug/deps/hana_diegetic-b1b1c996f3b409b2` | 2026-10-06 08:34:58 | 7 in 4 functions | 0 |
| after: `target/debug/deps/hana_diegetic-4e978e40151ca765` | 2026-10-06 10:11:13 | 0 | 80 in 10 functions |

- Before, refs per function: naga constant_evaluator math closure#35 3; `<f32>::mul_add` 1; `<f64>::mul_add` 2; `<glam::f32::vec2::Vec2>::mul_add` 1. compiler_builtins holds 2 `vfmadd` in 2 functions, the runtime `fmaf_with_fma`/`fma_with_fma`.
- After, `vfmadd` per function: `<f32>::mul_add` 1; `<f64>::mul_add` 2; `<glam::f32::vec2::Vec2>::mul_add` 2; six `image::metadata::cicp::CicpRgb::cast_pixels_by_fallback::<…>` 12 each; `naga::proc::constant_evaluator::component_wise_float::<3, 1>` 3. No compiler_builtins fma symbol is linked.

`hana_diegetic::render::fill_batch::dim_control_color` (`fill_batch.rs:2009`, `color.red.mul_add(0.2126, color.green.mul_add(0.7152, color.blue * 0.0722))`) calls `<f32>::mul_add` out of line twice in both binaries (`call <<f32>::mul_add>`; the after build has `vmulss` beside it). The callee:
- before, `<f32>::mul_add` @5691b20: `push %rax` / `mov 0x51a0030(%rip),%rax  # <fmaf$got>` / `call *%rax` / `pop %rax` / `ret`
- after, `<f32>::mul_add` @65f8af0: `vmovss %xmm1,-0x4(%rsp)` / `vmovaps %xmm0,%xmm1` / `vmovss -0x4(%rsp),%xmm0` / `vfmadd213ss %xmm2,%xmm1,%xmm0` / `ret`

**Files:**
- `scripts/buildlog/rust_release.py` — the clippy step's rustflags string (line 356).
- `scripts/buildlog/test_rust_release.py` — the expected string (line 272).
- `docs/as-built/buildlog-tests-per-edit-rust-release.md` — the `clippy` step row (line 76) names the new string.

**Gotchas:**
- hana debug builds do not inline `mul_add`: callers call `<f32>::mul_add` out of line, so the per-function proof is in that callee, not the caller.
- objdump on a linked binary names the GOT slot `<fmaf$got>`; `fmaf@GOTPCREL` is assembler text from `--emit=asm` and never appears in `objdump -d` output.
- After the flag, compiler_builtins' `fmaf` is not linked at all, because nothing calls it; its absence is the expected state.
- On natedev the clippy run passes each target flag twice with the same values, the environment string plus the user `~/.cargo/config.toml` table, by design; CI's environment alone passes each once.

### Phase 3 — Claude edits that leave a float multiply-add are told to write mul_add · status: done

#### As-built

- `mul_add_lib.py` reuses `fn_length_lib`'s scanner. `suboptimal_flops_scope(path) -> SuboptimalFlopsLintScope` reads the package's or workspace's `suboptimal_flops` level, else `nursery`; `ScopeState` is `enabled`, `disabled`, `exempt` (a `#![no_std]` root) or `no_package`, and every state but `enabled` passes silently. `float_mul_add_findings(path) -> tuple[SuboptimalFlopsLintScope, list[FloatMulAddFinding]]`; each finding has `line`, `a`, `b`, `c`, `expr` and `rewrite`. Both types are `__slots__` classes with read-only properties.
- Shapes, as clippy's `mul_add.rs` flags them: `a * b + c`, `c + a * b`, `a * b - c`, `c - a * b`, `x += a * b`, `x -= a * b`. An additive chain keeps clippy's whole span (`a - b + 2.0 * v` → `v.mul_add(2.0, a - b)`); a leading unary minus folds into the receiver (`-x * 0.5 + 1.0` → `(-x).mul_add(0.5, 1.0)`).
- Float proof: a float-literal operand proves the product when the other operand is not excluded (a non-float annotation, a pattern or closure binding, a reference or iterator binding, a glam vector constructor, a non-float file `const`/`static`) and each named field in it is declared `f32`/`f64` in a struct or enum of the file. Both operands typed `f32`/`f64` in the function, or cast `as f32`/`as f64`, also prove it.
- Rewrite: `a.mul_add(b, c)`; `a * b - c` → `a.mul_add(b, -c)`; `c - a * b` → `(-a).mul_add(b, c)`. The receiver is never an unsuffixed literal: the other operand becomes the receiver (`2.0 * v + c` → `v.mul_add(2.0, c)`), and nothing is reported when both operands are unsuffixed literals.
- Skips: const items, `const fn` bodies, `const { }` blocks, `static`/`const` initializers, macro bodies, a `*`-sum that is the receiver of a `hypot` `.sqrt()`, `allow`/`expect` (`cfg_attr` forms included) naming `clippy::suboptimal_flops` or `clippy::nursery` on the item, an enclosing item or the file, and an operand or addend that is indexed (`b[0]`), called, or followed by a field path or `::` (skipped whole, never cut short).
- `post-tool-use-mul-add.py` returns early when the file lacks `*` or `+`/`-`, imports the detector lazily, and blocks with reason `<path>:<line>: write <rewrite> for <expr> (clippy::suboptimal_flops)` per finding, joined by `; `, then ` The edit was applied.`, plus `additionalContext` and `systemMessage` `mul_add: <file> has N float multiply-add expression(s)`. Each finding appends `at`, `agent: "claude"`, `tool`, `cwd`, `file`, `line`, `expression` to `MUL_ADD_HOOK_STATE` or `~/.local/state/mul-add-hook/blocks.jsonl`. An internal error prints `systemMessage` `mul_add hook error: <type>: <message>` and exits 0; the edit stands.
- `settings.json`: the handler is third in the `Edit|MultiEdit|Write` group, right after fn-length.
- Measured: 0 findings over 1,544 hana `origin/main` files; 35 of 291 historical positions (12.0%) flagged in stub functions; on a nightly-clippy oracle crate, 17 of 18 diagnostics found on clippy's lines, 0 mismatches, every rewrite compiling. Per-edit cost is +23.14 ms CPU over bare Python on a 236-line file (+31.26 ms on 945 lines), above the 20 ms budget; the user accepted it on 2026-10-06.

**Files:**
- `scripts/hooks/mul_add_lib.py` — the detector and lint scope
- `scripts/hooks/post-tool-use-mul-add.py` — the Claude hook entry
- `scripts/hooks/test_mul_add.py` — 35 tests; pins mul-add right after fn-length
- `scripts/hooks/test_fn_length.py` — its registration test checks only the group's first two handlers
- `settings.json` — one handler

**Binds later work:**
- "Codex seats get the same block after `apply_patch`" calls the same `float_mul_add_findings`, within the accepted +23 ms. It makes public the ten private `fn_length_lib` names `mul_add_lib.py` imports behind one line-level `reportPrivateUsage` ignore (`_Token`, `_body_opener`, `_level`, `_macro_before`, `_pairs`, `_parents`, `_read_text`, `_read_toml`, `_table`, `_tokens`), owns the direct test of the hook-error `systemMessage`, and fixes no_std: `suboptimal_flops_scope` matches only the exact `#![no_std]` in a `lib.rs`/`main.rs` root, missing `#![ no_std ]` and `#![cfg_attr(..., no_std)]`, and a `#![no_std]` lib root silences a std `main.rs` in the same package.
- "Re-measure: long functions and multiply-adds out of hana's clippy failures" matches residuals against `blocks.jsonl` records and classifies each by running the detector on its snippet in a stub function (reach: a lower bound of 35/291), not by whether it holds a literal. Its controlled edit declares the operand `f32` in the same function; an untyped `x * 0.5 + 1.0` is not proved.
- T_claude_mul_add (PDT) comes from the live smoke once the merge reaches `~/.claude` main: a Claude Code process started after it `Write`s `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }` into a scratchpad crate denying `nursery`; the reason appears and `blocks.jsonl` gains an `"agent": "claude"` line.

**Gotchas:**
- An unsuffixed float literal receiver does not compile (E0689); clippy writes `2.0f32.mul_add(...)`.
- Clippy requires both `*` operands float-typed, so `&f32`, closure parameters and `glam::Vec3` operands pass it.
- Most historical positions are `field * literal` or calls whose float type is not visible locally, which caps recall.
- The scan (9.69 ms on 236 lines) costs more than the import (6.49 ms); a whole-file pre-filter saves little, as 1,114 of 1,544 hana files hold both `*` and `+`/`-`.

**Ruled out:** the bare literal rule (any float-literal operand proves the product) — it flags operands clippy passes; frozen dataclasses — importing `dataclasses` is about 56% of import time; a silent error path — the error `systemMessage` mirrors the fn-length hook; cutting per-edit cost below +23 ms — the user accepted it.

### Phase 4 — Codex seats get the same block after `apply_patch` · status: done

#### As-built

- `post-tool-use-fn-length.py` accepts Codex `apply_patch` payloads beside Claude's. `_patch_files` reads `*** Add File:` and `*** Update File:` headers (`*** Move to:` replaces the path, `*** Delete File:` is skipped, paths deduplicated, `.rs` only); relative paths resolve against the payload `cwd`. `_payload` returns the private `AppliedRustEdits(agent, tool, cwd, files)` or `IgnoredEdit`, never an empty string or a bare optional path.
- Every affected file is checked. One block names every long function across them, and `blocks.jsonl` gets one record per affected file with `"agent": "codex"`. The reason keeps `The edit was applied.`, since it is the whole message Codex shows the model. The `systemMessage` keeps the single-file text (`fn-length: lib.rs has 1 function(s) over 100 lines`); for several files it gives the total, each file relative to `cwd`, and each limit when packages differ.
- `codex_hooks.py install|check` (typed, standard library; binary from `CODEX_BIN`, else `codex` on `PATH`, else `~/.local/bin/codex`; `CODEX_HOME`, else `~/.codex`) owns one handler: `PostToolUse`, matcher `apply_patch`, command `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"`, timeout 10. The command string is byte-identical to the `settings.json` fn-length entry, so the trust hash matches on both machines.
- `install` appends `{"matcher": "apply_patch", "hooks": [<handler>]}` at the end of `hooks.PostToolUse` only when no `apply_patch` group holds an equal handler, so existing indexes and their recorded trust stay put (the Mac's `hooks.json` has PostToolUse groups 0 and 1, already trusted). The write is atomic (temp file in the same directory, then `os.replace`); invalid JSON exits 1 naming the file, which stays untouched. Trust goes through `codex app-server` over stdio with cwd `$HOME`: `initialize`, `hooks/list`, a `config/batchWrite` upsert of `hooks.state.<key>.trusted_hash` when the entry is `untrusted` or `modified`, then a second list, all bounded at 30 s with the server killed on exit. Exit 0 prints `trusted <key>` only when the entry reads trusted and enabled; otherwise exit 1 names the status or the server's RPC error.
- `check` lists without writing: exit 0 when trusted and enabled, else exit 1 with `fn-length codex hook: <absent|untrusted|modified|disabled>`. `codex_mesh.py` is unchanged; Codex sessions read user-layer hooks with no override.

**Files:**
- `scripts/hooks/post-tool-use-fn-length.py` — Claude and Codex payloads for the fn-length hook
- `scripts/hooks/codex_hooks.py` — installs and trusts the Codex hook, checks it
- `scripts/hooks/test_fn_length.py` — `apply_patch` cases among its 47 tests
- `scripts/hooks/test_codex_hooks.py` — 11 tests against a stub `codex` alone on `PATH` answering JSON-RPC from a state file

**Binds later work:** Install and smoke run only after the merge reaches `~/.claude` main and the Mac has pulled. Install is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/codex_hooks.py" install`, then `check`, on natedev and on the Mac over `ssh mac` printing `rc=$?` inside the command. The smoke is `codex exec` on both machines: a scratchpad crate denying `pedantic`, one function of 101 statement lines added with `apply_patch`, the reason in the output and an `"agent": "codex"` line in `blocks.jsonl`. T_codex is the time both smokes pass; it is G3, the condition the Codex mul_add work waits on.

**Gotchas:**
- The app-server child runs with cwd `$HOME`, so `CODEX_BIN`, a relative `PATH` entry and `CODEX_HOME` are made absolute before spawning, and the child gets the absolute `CODEX_HOME` in its env.
- Duplicate groups yield several matching `hooks/list` entries: a trusted and enabled match wins, else the first trusted, else the first; install trusts exactly one, and only when none is trusted.

### Phase 5 — Codex patches that leave a float multiply-add are told to write mul_add · status: done

#### As-built

- `fn_length_lib.py` holds the shared payload parser, `applied_rust_edits(raw) -> AppliedRustEdits | IgnoredEdit` (`AppliedRustEdits(agent, tool, cwd, files)`), for Claude `Edit|MultiEdit|Write` and Codex `apply_patch`, and `applied_patch_rust_files(patch)`: Add and Update headers, `*** Move to:` replaces the path, Delete skipped, deduplicated, `.rs` only. Both hook entries call it; neither imports it, nor `typing`, before its `.rs` pre-check (module constant `TYPE_CHECKING = False`).
- Public scanner names in `fn_length_lib.py` (`RustToken`, `tokenize_rust`, `pair_delimiters`, `macro_before`, `read_text`, …); `mul_add_lib.py` imports them with no ignore.
- One folded scope reader, `read_clippy_lint(path, lint, group) -> ConfiguredClippyLint | UnavailableClippyLint`, with `ClippyLintLevel`. Each hook keeps its own scope type: `SuboptimalFlopsLintScope` (mul_add) and `TooManyLinesLintScope` (fn-length). An unreadable manifest passes both hooks.
- `target_may_be_no_std(path, package_dir)`: `src/main.rs`, `src/bin/**` and a directory target's `main.rs` (`src/bin/<n>/main.rs`, `tests|examples|benches/<n>/main.rs`) are their own roots; `tests/`, `examples/`, `benches/` files are their own crates; any other `src/` file belongs to `lib.rs`, else `main.rs`, and passes when either root is no_std. A root is never in doubt: a std `main.rs` blocks beside a no_std library. `#![no_std]` with any spacing and `#![cfg_attr(<cond>, no_std)]` count; comments and strings do not.
- A file with no resolvable root (`build.rs`, a file outside `src/`, `tests/`, `examples/`, `benches/`, a `src/` file with neither `lib.rs` nor `main.rs`) reads `exempt` and passes.
- `post-tool-use-mul-add.py` accepts `apply_patch`: one block across files in patch order; `systemMessage` is `mul_add: <file> has N float multiply-add expression(s)` for one file, else `N float multiply-add expression(s) in <files>` relative to `cwd`. One `blocks.jsonl` record per finding (`agent`, `tool`, `cwd`, `file`, `line`, `expression`), written only once every file is scanned.
- `codex_hooks.py` installs and trusts two `apply_patch` handlers (fn-length, mul_add), appending groups, never renumbering. `check` prints `fn-length codex hook: <status>` and `mul_add codex hook: <status>`, exit 0 only when both are trusted and enabled.
- CPU over bare Python, 20 interleaved runs: mul_add Claude edit on a 236-line file +13.19 ms before vs +14.13 ms after; Codex three-file patch +14.17 ms. fn-length Claude edit +13.05 vs +13.12 ms; Codex three-file +14.30 vs +14.16 ms. Non-Rust edit: mul_add +1.07 vs +1.03 ms, fn-length +1.77 vs +1.17 ms.

**Files:**
- `scripts/hooks/fn_length_lib.py` — shared payload parser and types, public scanner, folded scope reader, per-target no_std
- `scripts/hooks/mul_add_lib.py` — public scanner imports; scope through the shared reader
- `scripts/hooks/post-tool-use-mul-add.py` — Claude and Codex payloads
- `scripts/hooks/post-tool-use-fn-length.py` — calls the shared parser
- `scripts/hooks/codex_hooks.py` — install and `check` for both Codex hooks
- `scripts/hooks/test_mul_add.py` — 44 tests
- `scripts/hooks/test_fn_length.py` — 50 tests
- `scripts/hooks/test_codex_hooks.py` — 15 tests

**Binds later work:**
- Once the branch is merged into `~/.claude` main and the Mac has pulled: `codex_hooks.py install`, then `check`, on natedev (`dangerouslyDisableSandbox`) and on the Mac over `ssh mac`, printing `rc=$?` inside the command. Both report both hooks trusted and the existing groups unchanged.
- A `codex exec` smoke in a scratchpad crate denying `nursery` writes `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }` via `apply_patch` and shows the reason and an `"agent": "codex"` log line. T_codex_mul_add, recorded in PDT, was 2026-10-06 15:00:51 PDT; the re-measure's 96-hour window starts at T_detector instead (showrunner, 2026-10-06).
- Done right after T_codex_mul_add, and repeated by Phase 6's go-live step at T_detector for the new window: that re-measure's controls on natedev and on the Mac: a Claude edit leaving `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }` in a crate denying `nursery`; a Claude edit and a Codex patch each adding a function of 101 statement lines in a crate denying `pedantic`. The Codex multiply-add smoke is the mul_add hook's Codex control.
- `exempt` on an unresolvable root narrows the mul_add hook's reach; the re-measure's detector-reach split reports it.

**Gotchas:**
- A module-level import in a hook entry costs every non-Rust edit (`fn_length_lib` about 5 ms, `typing` 2.9 ms).
- A hook fixture crate needs `src/lib.rs` (or `main.rs`), or every file in it reads `exempt`.
- `#![cfg_attr(c, no_std, allow(x))]` (no_std not last) reads std; left as is.

**Ruled out:**
- Reading an unknown level as unreadable rather than unset — the verdict is pass either way.
- Parsing a manifest without the text `lints` — it cannot set the lint, so the text pre-check stays.

### Phase 6 — The detector reads `let x = if … else …` as a plain binding, and proves file float constants and field chains · status: done

#### As-built

- Let-else: `_has_let_else(source, start)` counts an `else` only at the `let` statement's own bracket depth and only after a token other than `}` (Rust rejects a let-else initializer ending in `}`). `_LET_ELSE` and the let-else alternative of `_COMPOUND_PATTERN` use it. `let w = if c { 1.0 } else { 2.0 };` and `let w = if c { a } else if d { b } else { e };` are ordinary bindings, so `w` is no longer excluded. `let Some(v) = opt else { return };` and `let Some(v) = f(if c { 1 } else { 2 }) else { return };` still exclude their bindings. The if-initialized vector binding stays excluded through `_VECTOR_BINDING`.
- File floats: `const`/`static` names typed exactly `f32` or `f64` join each function's `scalars`, unless the function annotates the name or a plain local `let` of the same name shadows it. File-level declarations (outside every function body) are shared by every function; a non-float file-level declaration of a name wins over a float one of the same name, in either order; a declaration inside a function body replaces it for that function only. `const K: f32 = 2.0; fn f(x: f32, c: f32) -> f32 { c + x * K }` gives `x.mul_add(K, c)`; `const K: Vec2 = Vec2::ONE;` gives nothing.
- Field chains: `field_is_scalar` checks the last field only, against `scalar_fields` (names declared `f32`/`f64` in any struct or enum body of the file), for each operand and the addend. With no float literal, a field-chain operand without a call and with a scalar last field proves itself, and its last field must also be absent from `nonfloat_fields` (names the file declares with a non-float type in any struct or enum body): a name declared both `f32` and non-float would otherwise prove an integer product. The receiver regex is `[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|\d[\w.]*`, so field-chain receivers print without parentheses: `s.a.mul_add(s.b, c)`, `(-s.inner.h).mul_add(0.5, c)`.
- `float_mul_add_findings(path)` keeps its signature. No new module, flag or environment variable; the hook entry, the scope reader and the block format are unchanged.
- `test_mul_add.py` holds 68 tests (44 before): one per example above plus regressions for the constant-scope and field-name-collision rules.
- Measured at ship: 68/68 tests; basedpyright 0 errors, 0 warnings, 0 notes on both files; hana `origin/main` `ce51498c5` (1,544 files) zero findings; oracle 17 detector findings against 18 Clippy diagnostics, 0 unmatched, missed position 25 pre-existing; recall 32 → 37 of 291 through `detect_root.py` (new 70, 75, 81, 172, 190; none lost; 81 caught); per-edit cost +0.15 ms on a 236-line file and +0.60 ms on a 945-line file.

**Files:**
- `scripts/hooks/mul_add_lib.py` — the let-else rule, file floats with per-function constant scope, the last-field rule and the nonfloat-field veto
- `scripts/hooks/test_mul_add.py` — 68 tests

**Binds later work:**
- The detector the re-measure measures is this one.
- Go-live is the unit director's step after the merge and the pulls: a Claude edit and a Codex patch, each on natedev and on the Mac, leave `const K: f32 = 2.0; fn f(x: f32, c: f32) -> f32 { c + x * K }` in a scratch crate with `src/lib.rs` denying `nursery`, and each shows `x.mul_add(K, c)` plus a `blocks.jsonl` line naming the agent. T_detector (PDT) is the end of the last of the four, sent to natedev; the re-measure's window opens there.
- Go-live ran as specified, and each of its four runs showed `x.mul_add(K, c)` and a log line naming the agent: natedev's Claude edit ended 2026-10-06 18:22:53 PDT and its Codex patch 18:22:54, the Mac's Codex patch 18:48:17, the Mac's Claude edit 22:44:05. The Mac's Claude login sits in a login keychain that stays locked over ssh, so its run went through a Terminal window opened in the logged-in session, with the full login `PATH` set; two earlier launches never reached the hook, one without `cargo-berth` on `PATH` and one without `claude`.
- T_detector: 2026-10-06 22:44:05 PDT, the end of the Mac Claude run, the last of the four go-live runs. W runs to 2026-10-10 22:44:05 PDT (01:44:05 EDT on 2026-10-11).
- The eight controls ran right after it, each showing its hook's block and a log line naming the agent, all inside W (log times UTC):

  | Machine | Agent | Hook | Run (PDT) | Log line |
  | --- | --- | --- | --- | --- |
  | Mac | Claude | mul_add | 22:44:05–22:44:12 | 05:44:09Z |
  | Mac | Claude | fn-length | 22:44:12–22:44:25 | 05:44:22Z |
  | Mac | Codex | mul_add | 22:45:37–22:45:53 | 05:45:48Z |
  | Mac | Codex | fn-length | 22:45:53–22:46:59 | 05:46:33Z |
  | natedev | Claude | mul_add | 22:45:05–22:45:12 | 05:45:09Z |
  | natedev | Claude | fn-length | 22:45:12–22:45:24 | 05:45:20Z |
  | natedev | Codex | mul_add | 22:45:24–22:45:50 | 05:45:45Z |
  | natedev | Codex | fn-length | 22:45:50–22:46:22 | 05:46:17Z |

  The mul_add blocks read `write x.mul_add(0.5, 1.0) for x * 0.5 + 1.0 (clippy::suboptimal_flops)` at `src/lib.rs` line 1 for Claude, 5 for natedev's Codex and 4 for the Mac's. The fn-length blocks read `fn long_sum at src/lib.rs:N is 101 lines (limit 100): split it now.` with N 1 for Claude and 5 for Codex; the Mac's Codex split the function afterwards, and its log line records the 101-line version. The control rows carry these `cwd` values, so the re-measure leaves them out of its rates: on natedev `…/scratchpad/ctl_after/{claude/muladd, claude/fnlen, codex_muladd, codex_fnlen}`, on the Mac `~/controls-muladd/{muladd, fnlen}`, `~/ctl-codex-muladd` and `~/ctl-codex-fnlen`; the go-live rows carry `…/scratchpad/golive/{claude_crate, codex_crate}` and `~/golive-claude`, `~/golive-codex`.

**Gotchas:**
- `detect.py` builds stub crates with no `src/lib.rs`, so Phase 5's reader exempts them and every position reads uncaught; use `detect_root.py`, which adds an empty `src/lib.rs`.
- Stub recall is not real-file reach: in a real file the nonfloat-field veto and the non-float file-level constant rule can remove a finding the stub shows.
- A let-else whose initializer holds a nested `;` (`let Some(v) = ({ let x = opt; x }) else { return 0.0 };`) is not read as a let-else: `_LET_ELSE` and the let-else alternative of `_COMPOUND_PATTERN` stop at the inner `;`, so `v * 0.5 + 1.0` is flagged. It predates this phase.

**Ruled out:**
- An enclosing function seeing a nested function's constants — no leak occurred in practice and the rule added nothing.
- Dropping the nonfloat-field veto — a field name declared both `f32` and non-float proves an integer product.

### Phase 7 — Codex seats' build rows carry the seat's own thread id · status: done

#### As-built

- `who()` in `scripts/buildlog/record.py` stores `env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID") or None` as `session`. A Codex seat inherits its director's Claude session id, so the thread id comes first and a Codex seat's rows carry the seat's own id. A Claude seat's rows are unchanged, an environment with neither id stores `None`, `caller` is unchanged, and no recorded row is rewritten.
- `test_session_prefers_codex_thread_id` in `scripts/buildlog/test_record.py` records under an environment holding both ids (stores the thread id), only the Claude id (stores it) and neither (stores `None`).
- `report.py` groups on `coalesce(delegate_session, session)` and `loop.py` groups failed calls by seat and session; neither assumes the Claude id wins. `scripts/agents/agents_config.sh` (`_agents_caller_family`) already lets Codex win when both ids are present.
- Measured at ship: the buildlog suite, 255 tests, passes; basedpyright reports 0 errors and 0 warnings on both files.

**Files:**
- `scripts/buildlog/record.py` — the session precedence in `who()` (the follow-ups unit's file, edited at the showrunner's word)
- `scripts/buildlog/test_record.py` — the three-case test

**Binds later work:**
- Rows a Codex seat recorded before this change merged name the director's Claude session; rows after it name the seat's thread. It is live on natedev from 2026-10-06 19:24:53 PDT and on the Mac from 19:55:47 PDT, when it pulled `~/.claude`. The re-measure states which rows of its window came after, and does not compare a Codex seat's `loop.py` gaps across that line.

**Ruled out:**
- Rewriting rows already recorded — the change fixes the precedence only.

### Phase 8 — The detector's scaling test holds under machine load · status: done

#### As-built

- `test_many_findings_scan_scales_with_file_length` in `scripts/hooks/test_mul_add.py` scans a 200-line and an 800-line file five times each with `time.perf_counter()`, keeps the fastest run of each size (load only ever adds time), and asserts the 800-line time is under 10× the 200-line time. Over 4× the lines a linear scan takes about 4× as long and a quadratic one about 16×, which the test's comment states. The finding-count and last-line asserts run on every sample.
- `float_mul_add_findings(path)` and `mul_add_lib.py` are unchanged.
- Measured at ship: 20 passes in a row under a CPU load on all 32 cores; a scratch 4× bound fails, so the test still measures; the `test_mul_add.py` suite passes and basedpyright reports 0 errors, 0 warnings and 0 notes.

**Files:**
- `scripts/hooks/test_mul_add.py` — the scaling test

**Gotchas:**
- Running the test by its dotted name needs `PYTHONPATH=scripts/hooks`, since the test imports `mul_add_lib` as a sibling.

**Ruled out:**
- A count-based check — no unit of the scanner's work is reachable from the test without editing `mul_add_lib.py`.

### Phase 9 — Re-measure: long functions and multiply-adds out of hana's clippy failures · status: done

#### As-built

Re-measured on natedev at 2026-10-10 22:59:12 PDT. W is the same 96-hour, start-inclusive/end-exclusive window for both lints: **2026-10-06 22:44:05 PDT to 2026-10-10 22:44:05 PDT**. It opens at Phase 6's last go-live run, so the detector is Phase 6's and both hooks cover Claude and Codex on both machines throughout W. The earlier Phase 5 controls and all go-live/control scratch crates are excluded from the hana rates.

**Verdicts: neither lint meets all three success conditions.** `suboptimal_flops` fails both rate and seat-time conditions; `too_many_lines` passes seat time but narrowly fails the failure-rate condition. Both lints pass all four machine/agent controls. The 75% thresholds remain unchanged.

| Measure | `suboptimal_flops` before | `suboptimal_flops` W | `too_many_lines` before | `too_many_lines` W |
| --- | ---: | ---: | ---: | ---: |
| Failed steps per 100 hana clippy steps | 7.916 | **8.125819** (62/763); limit ≤ 1.979: **FAIL** | 13.3 | **3.538663** (27/763); limit ≤ 3.3: **FAIL** |
| Sole-cause failed steps per 100 | 2.573 | **1.572739** (12/763) | 4.9 | **0.393185** (3/763) |
| Sole-cause seat hours per day | 0.402 | **0.576189**; limit ≤ 0.100: **FAIL** | 1.07 | **0.115689**; limit ≤ 0.27: **PASS** |
| Controls: Claude and Codex, natedev and Mac | — | **4/4 PASS**; record and delivered block text for each | — | **4/4 PASS**; record and delivered block text for each |
| Global verdict | — | **FAIL**; confirmed detector reach 0/763, snippet-only reach 3/763 = **0.393185 per 100** | — | **FAIL** |

The multiply-add failure rate rose 2.650570% and its seat time rose 43.330585% against Phase 1. Long-function failures fell 73.393510% and seat time fell 89.187976% against the fn-length plan's Phase 1 (2026-10-06 08:17 PDT; span 3.96 days). Sole-cause rates have no separate success threshold.

**Denominator and cost.** The read-only totals query found **763** hana clippy steps, **250** failed. All 250 have retained logs and an error diagnostic: **0** missing logs, **0** without diagnostics. W's first hana clippy step is **2026-10-06 22:44:37.901 PDT**; its span to W's end is **345,567.099 seconds = 3.9996192013888887 days**, the divisor for seat hours per day.

- `suboptimal_flops`: **159** diagnostics, all multiply-add messages, across **62** failed steps; **116** src-path and **43** test/example-path diagnostics. Of the associated calls, **61** are `lint` and **1** `example`. **12** sole-cause steps/calls, none lacking a call record; failed-call wall **2,574 s = 0.715 h**, median **186 s**, clippy-step wall **757.327 s**. **11** next lint calls within the **1,800 s** cap; gap median **305.703 s**, p75 **1,053.247 s**, total **5,722.331 s = 1.589536389 h**; **10** passed, **1** failed again, **1** had no follow-up within the cap. Seat cost **8,296.331 s = 2.304536389 h**, divided by the measured span. Unrounded script-derived daily cost is **0.576188950233001 h**.
- `too_many_lines`: **27** diagnostics in **27** failed steps; **21** src-path and **6** test/example-path diagnostics, all associated with `lint` calls. **3** sole-cause steps/calls, none lacking a call record; failed-call wall **860 s = 0.238888889 h**, median **213 s**, clippy-step wall **387.049 s**. All **3** have a follow-up within **1,800 s**; gap median **244.217 s**, p75 **351.503 s**, total **805.758 s = 0.223821667 h**; **2** passed, **1** failed again, **0** lack follow-up. Seat cost **1,665.758 s = 0.462710556 h**, divided by the measured span. Unrounded daily cost is **0.11568865241783369 h**.
- `tml_lengths.py`: **10** distinct file:line functions after collapsing repeated steps; lengths p25 **101**, median **103**, p75 **105**, p90/max **1,872**. **1** over each of **150**, **200**, and **300** lines; **4/10** in test files by path. It ran because the lint census was nonzero.

**Controls.** The eight live `blocks.jsonl` records were read locally and over `ssh mac`, then matched to the saved start/end and actual block reason in `scratchpad/ctl_after/{natedev_controls.txt,mac_claude_golive_and_controls.txt,mac_codex_controls.txt}`. All runs below are on **2026-10-06**, and every displayed time is **PDT**. Each multiply-add record says `x * 0.5 + 1.0`; delivered feedback says `write x.mul_add(0.5, 1.0) ... (clippy::suboptimal_flops)`. Each fn-length record names `long_sum`, **101** lines, limit **100**, with delivered `split it now` feedback. Each Codex record names `agent: codex`, `tool: apply_patch`; each Claude record names `agent: claude`, `tool: Write`.

| Machine | Agent | Hook | Saved run start–end (PDT) | Live record time (PDT) | Line | Proven |
| --- | --- | --- | --- | --- | ---: | --- |
| natedev | Claude | mul_add | 22:45:05–22:45:12 | 22:45:09.493604 | 1 | yes |
| natedev | Codex | mul_add | 22:45:24–22:45:50 | 22:45:45.017466 | 5 | yes |
| Mac | Claude | mul_add | 22:44:05–22:44:12 | 22:44:09.000212 | 1 | yes |
| Mac | Codex | mul_add | 22:45:37–22:45:53 | 22:45:48.760633 | 4 | yes |
| natedev | Claude | fn-length | 22:45:12–22:45:24 | 22:45:20.848400 | 1 | yes |
| natedev | Codex | fn-length | 22:45:50–22:46:22 | 22:46:17.834045 | 5 | yes |
| Mac | Claude | fn-length | 22:44:12–22:44:25 | 22:44:22.078640 | 1 | yes |
| Mac | Codex | fn-length | 22:45:53–22:46:59 | 22:46:33.666409 | 5 | yes |

Inside W the live logs contain **12** natedev mul_add records (**1** Claude, **11** Codex), **312** natedev fn-length records (**7** Claude, **305** Codex), and **2** Mac records per hook (**1** Claude and **1** Codex each). These are all-project log counts, not hana intervention rates. The Mac Codex fn-length control split the function after its proven block; the original 101-line version remains in the record.

**Multiply-add residuals and detector reach.** Every one of the **159** diagnostics has a per-step record in the scratch ledger, with original buildlog row, quoted source, resolved worktree-relative path, preceding blocks, and detector result. There are **37** normalized files (**39** printed path spellings), **44** HEAD shas and **59** tree keys among the **62** steps. **14** diagnostics in **8** steps have an earlier block for the same file; **145** do not. Matching the preceding block's time, absolute file, line and expression gives **0** exact matches and **159** unmatched diagnostics. An earlier block elsewhere in the file does not prove that this expression was blocked.

| Residual scan | Diagnostics | Detector flags this expression | Does not flag | Scope |
| --- | ---: | ---: | ---: | --- |
| Commit file, diagnostic line and all printed source lines match | 40 | 0 | 40 | 33 enabled, 7 exempt |
| Printed snippet in a stub function, HEAD line differs | 105 | 4 | 101 | snippet-only lower-bound classification |
| Printed snippet in a stub function, file absent at HEAD | 14 | 0 | 14 | snippet-only lower-bound classification |
| All snippet-only scans | 119 | 4 | 115 | all enabled; empty `src/lib.rs` present |
| Total | 159 | 4 snippet-only | 155 | 0 unscannable |

The **40** real-file scans used the file from `git show <sha>:<file>` at its own path inside a `git archive` checkout of that commit, with its actual package/workspace declarations and roots. The **7** exempt diagnostics are in `crates/hana/tests/camera_panel_rear_lifecycle/mod.rs` (**3**), `crates/hana_organon/tests/arc_chooser/mod.rs` (**2**), `crates/hana/tests/demo_moments/cable_pixels.rs` (**1**), and `crates/hana/tests/open_assembly_card/mod.rs` (**1**). The stub fallback adds no invented parameter types or file declarations; **3** snippets required delimiter balancing, **1** is truncated in the step log, and no scan remained unbalanced.

The four snippet hits are `crates/hana_origami/tests/unfold/app.rs:1546`, `0.1125 - 0.5 * meters_per_pixel` (**2** diagnostics in **2** steps), and `crates/hana_diegetic/tests/slider_face.rs:671`, `(full_right - full_left) - 2.0 * (half_right - half_left)`, plus line **701**, `(end_right - end_left) - 0.6 * (full_right - full_left)` (**2** diagnostics in **1** step). None has a preceding matching block. Thus confirmed real-file reached failures are **0/763 = 0 per 100**, and separately the snippet-only lower-bound classification reaches **3/763 = 0.3931847968545216 per 100**. The latter does not prove actual file reach: omitted declarations can supply types or veto a stub hit. HEAD is not the build's uncommitted tree; no live hana working-tree source was read and no mismatching diagnostic was promoted to a real-file result. The global thresholds and FAIL verdict remain in force.

**Long-function residuals.** Resolved absolute `record.file` was compared with each diagnostic's step worktree plus its normalized file path, not with the editor's `cwd`. An earlier block must name the diagnostic's function in `functions`. **5/27** diagnostics matched: the hook fired and a later lint still found that function long. **22/27** are `unknown`; **0** are proven shell/person/rustfmt edits unseen by the hook, and **0** are proven macro-body exclusions. Of the unknowns, **19** have a matching commit line and no enclosing macro body; **3** lack a matching HEAD source (**2** absent files and **1** different line). The retained step logs and rows do not identify the editing mechanism, so unmatched does not itself mean the hook failed.

| Matched function | Normalized file | Failed diagnostics | Latest earlier block (PDT) | Failed step start (PDT) |
| --- | --- | ---: | --- | --- |
| `format_operator_row` | `crates/hana/src/status_panel/display.rs` | 1 | 2026-10-07 02:55:28.436529 | 2026-10-07 03:43:05.579 |
| `route_semantic_input` | `crates/hana_diegetic/src/widgets/interaction/input/routing.rs` | 1 | 2026-10-08 07:00:03.361745 | 2026-10-08 13:38:58.821 |
| `build_drawing` | `crates/hana_origami/src/tool.rs` | 2 | 2026-10-10 12:45:19.471249 | 2026-10-10 16:17:37.731 and 16:17:41.064 |
| `assert_add_camera_palette_icon` | `crates/hana/tests/control_panel/mod.rs` | 1 | 2026-10-10 21:02:15.369876 | 2026-10-10 21:17:34.105 |

The five most frequent normalized residual files are `crates/hana/src/tool/geometry.rs` (**14**), `crates/hana/tests/open_assembly_card/mod.rs` (**3**), `crates/hana/src/selection/select_on_spawn.rs` (**2**), `crates/hana_origami/src/tool.rs` (**2**), and `crates/hana/src/status_panel/display.rs` (**1**, tied with five other files). These five account for **22/27** diagnostics.

**Session-change boundary and sample coverage.** The queried natedev index has **13,048** hana step rows and **9,145** hana call rows inside W, all after Phase 7's **2026-10-06 19:24:53 PDT** natedev and **19:55:47 PDT** Mac go-live times; the timestamp audit found **0** pre-change rows. Its earliest hana step is **2026-10-06 22:44:35.271 PDT**, earliest hana call **22:46:26.014 PDT**. The Mac's read-only index contains an older replicated subset of **291** W clippy steps (**118** failed), from **2026-10-06 22:44:37.901 PDT** through **2026-10-08 09:32:37.976 PDT**, also entirely after both change times. All W rows present in either index originate on natedev; no Mac-origin W clippy row is present. The Mac subset was not added to the denominator. Loop comparisons use only W-started calls and cannot cross the session-change line. This measures the available indexed sample; it does not independently prove that no unindexed Mac run exists.

**FMA in CI.** `git -C ~/rust/hana show origin/main:.github/workflows/ci.yml`, at local `origin/main` **`ce51498c50b1fc79b44bcc50dcd083e205929467`**, does **not** carry `-C target-cpu=x86-64-v3`. Its Linux target rustflags remain `-C link-arg=-fuse-ld=mold`. The director sent natedev both verdicts, with this missing flag, at 2026-10-10 23:14 PDT.

**Reproduction and verification.** Before measuring, all four SHA-256 hashes matched:

| Read-only original script | SHA-256 |
| --- | --- |
| `cost.py` | `c2de20870ab2de9b5438e469c565d25c48185438ab42dd77807d817655b5d4fa` |
| `loop.py` | `a775765b96311a783c8d6af8cdc925071b89b4ac423ae8ffa75b88fe81bf9abf` |
| `clippy_fail_lints.py` | `e178642d9bb1690562c132d824256198cc8f6e622b779b94ca76abbc75e841ce` |
| `tml_lengths.py` | `18754c36c8c545928a082e3bded49c688f029221b917b0f1e6c164028e4b6522` |

The only changes in the four scratch copies are their `started_at` bounds and corresponding SQL arguments, including `loop.py`'s follow-up call query. SQL `datetime` normalizes the recorded fractional timestamps and explicit **PDT** (`-07:00`) bounds. The full original-versus-copy diff is saved as `p9/bounded.diff` and shown below. The totals query uses the same bounds:

```sql
select count(*), sum(status!=0), min(started_at)
from steps
where repo='hana' and step='clippy'
  and datetime(started_at)>=datetime(?)
  and datetime(started_at)<datetime(?)
```

Each final requested command exited **0**, ran in the background under `set -o pipefail`, and had its output read and deleted before the next: `cost.py 4 suboptimal_flops`, `loop.py 4 suboptimal_flops`, `clippy_fail_lints.py 4`; `cost.py 4 too_many_lines`, `loop.py 4 too_many_lines`, `clippy_fail_lints.py 4`, `tml_lengths.py 4`; and the bounded totals query. `ssh mac`, the CI read, valid `git show` reads, all **18** complete-commit archives, and the final residual scan also exited **0**. Across both lints the source ledger covers **186** diagnostics and **51** HEAD shas. One checkout existed at a time, at most **197,287,777 bytes** (under **0.20 GB**), and each was deleted before the next. A preliminary search exited **127** after a shell `path` variable affected PATH; a first scratch residual helper exited **1** on an incorrect `macro_before` call. Both were corrected. A closeout-only arithmetic assertion also exited **1** because equivalent rate formulas differed by one floating-point rounding unit; its corrected tolerance check passed. The scripts, hooks and buildlog remained read-only, and all final required runs passed.

Scratch evidence is under `/tmp/claude-1000/-home-natepiano-worktrees-claude-build-followups-mul-add/69dc3a91-6876-4d57-adc3-5e921cb38ff1/scratchpad/p9/`: `measurements.json` (unrounded results), `totals.json`, `controls.json`, `hook_records.json`, `residuals_scanned.json` (every diagnostic and match), `archive_stats.json`, and the bounded script copies/diff. The only repository content change is this Phase 9 section. **Boundary exception:** the initial `git status` refreshed worktree Git index metadata at **2026-10-10 22:47:06.442395 PDT**; later status reads used `GIT_OPTIONAL_LOCKS=0`. No staged content change, commit, branch operation, hook/config edit, build or test run was made. Required board posts also append outside the repository to the phase board.

```diff
--- original/cost.py
+++ p9/cost.py
@@ -13,8 +13,8 @@
 db = sqlite3.connect(f"file:{root}/index.sqlite?mode=ro", uri=True)
 rows = db.execute(
     """select s.id, s.log, s.call_id, c.wall_s, c.verb, c.seat, s.started_at, s.duration_s from steps s left join calls c on c.id = s.call_id
-       where s.repo='hana' and s.step='clippy' and s.status!=0 and s.started_at>=datetime('now', ?)""",
-    (f"-{days} days",),
+       where s.repo='hana' and s.step='clippy' and s.status!=0 and datetime(s.started_at)>=datetime(?) and datetime(s.started_at)<datetime(?)""",
+    ('2026-10-06T22:44:05-07:00', '2026-10-10T22:44:05-07:00'),
 ).fetchall()
 LINT = re.compile(r"(?:#\[(?:deny|forbid)\((\S+?)\)\]|rust-clippy/[^#\s]*#(\w+)|`-D (\S+?)`|implied by `-D (\S+?)`)")
 CODE = re.compile(r"^error\[(E\d{4})\]")
--- original/loop.py
+++ p9/loop.py
@@ -37,11 +37,11 @@
         out.add(name)
     return out
 steps = db.execute("""select s.call_id, s.log from steps s where s.repo='hana' and s.step='clippy' and s.status!=0
-    and s.log is not null and s.call_id is not null and s.started_at>=datetime('now', ?)""", (f"-{days} days",)).fetchall()
+    and s.log is not null and s.call_id is not null and datetime(s.started_at)>=datetime(?) and datetime(s.started_at)<datetime(?)""", ('2026-10-06T22:44:05-07:00', '2026-10-10T22:44:05-07:00')).fetchall()
 sole = {cid for cid, log in steps if (ls := lints(log)) and ls <= target}
 calls = db.execute("""select id, started_at, ended_at, worktree, coalesce(seat,''), coalesce(session,''), outcome, wall_s
-    from calls where repo='hana' and tool='verify.sh' and verb='lint' and started_at>=datetime('now', ?) order by started_at""",
-    (f"-{int(days)+1} days",)).fetchall()
+    from calls where repo='hana' and tool='verify.sh' and verb='lint' and datetime(started_at)>=datetime(?) and datetime(started_at)<datetime(?) order by started_at""",
+    ('2026-10-06T22:44:05-07:00', '2026-10-10T22:44:05-07:00')).fetchall()
 gaps: list[float] = []; passed = 0; again = 0; none = 0; walls: list[float] = []
 for i, (cid, st, en, wt, seat, sess, out, wall) in enumerate(calls):
     if cid not in sole or not en:
--- original/clippy_fail_lints.py
+++ p9/clippy_fail_lints.py
@@ -13,8 +13,8 @@
 root = Path.home() / ".local/state/buildlog"
 db = sqlite3.connect(f"file:{root}/index.sqlite?mode=ro", uri=True)
 rows = db.execute(
-    "select id, log from steps where repo='hana' and step=? and status!=0 and started_at>=datetime('now', ?)",
-    (step, f"-{days} days"),
+    "select id, log from steps where repo='hana' and step=? and status!=0 and datetime(started_at)>=datetime(?) and datetime(started_at)<datetime(?)",
+    (step, '2026-10-06T22:44:05-07:00', '2026-10-10T22:44:05-07:00'),
 ).fetchall()
 LINT = re.compile(r"(?:#\[(?:deny|forbid)\((\S+?)\)\]|rust-clippy/[^#\s]*#(\w+)|`-D (\S+?)`|implied by `-D (\S+?)`)")
 CODE = re.compile(r"^error\[(E\d{4})\]")
--- original/tml_lengths.py
+++ p9/tml_lengths.py
@@ -10,7 +10,7 @@
 MSG = re.compile(r"^error: this function has too many lines \((\d+)/(\d+)\)")
 LOC = re.compile(r"^\s*--> (\S+?):(\d+):")
 seen: dict[str, int] = {}
-for (log,) in db.execute("select log from steps where repo='hana' and step='clippy' and status!=0 and log is not null and started_at>=datetime('now', ?)", (f"-{days} days",)):
+for (log,) in db.execute("select log from steps where repo='hana' and step='clippy' and status!=0 and log is not null and datetime(started_at)>=datetime(?) and datetime(started_at)<datetime(?)", ('2026-10-06T22:44:05-07:00', '2026-10-10T22:44:05-07:00')):
     lines = gzip.open(root / log, "rt", errors="replace").read().splitlines()
     for i, line in enumerate(lines):
         m = MSG.match(line)
```
