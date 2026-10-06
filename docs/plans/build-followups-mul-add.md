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
| G3 | Phase 4 | `stalls-unit` fn-length Codex phase (its Phase 7 since the 2026-10-06 renumbering) merged and installed | natedev sends its merge hash |
| G4 | Phase 5 | 96 hours after T_codex_mul_add (Phase 4's As-built) | the clock |

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

### Phase 4 — Codex seats get the same block after `apply_patch` · status: todo

#### Work Order

**Blocked by:** G3.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Goal:** a Codex `apply_patch` that leaves a float multiply-add reads the same reason, on natedev and the Mac.

**Constraints from prior phases:**
- Phase 3's detector API is `float_mul_add_findings(path) -> tuple[SuboptimalFlopsLintScope, list[FloatMulAddFinding]]` in `mul_add_lib.py`. The Codex entry calls it unchanged for each applied `.rs` path. Its types are `__slots__` classes with read-only properties, not dataclasses: importing `dataclasses` was about 56% of the import cost.
- Speed: the user accepted +23 ms CPU per Claude edit (measured on a 236-line real file). The Codex path adds only payload parsing.
- `mul_add_lib.py` imports `_Token, _body_opener, _level, _macro_before, _pairs, _parents, _read_text, _read_toml, _table, _tokens` from `fn_length_lib`, behind one line-level `# pyright: ignore[reportPrivateUsage]`.
- `test_fn_length.py`'s registration test checks only the first two handlers of the `Edit|MultiEdit|Write` group. `test_mul_add.py` pins mul-add right after fn-length.
- The registered hook commands run from `~/.claude` main. So the live install, `check` and smoke run only after this phase is merged into main, and on the Mac only after it has pulled.

**Spec:**
- `post-tool-use-mul-add.py` accepts `apply_patch` payloads exactly as the fn-length hook does, through one shared parser, logging `"agent": "codex"`. It checks each applied `.rs` path and joins the reasons across files in patch order.
- **Payload types.** The shared parser returns a semantic type, never strings with empty-string sentinels: `AppliedRustEdits` (tool, cwd, the applied `.rs` paths) or `IgnoredEdit` (any other payload). Both hook entries use it.
- `codex_hooks.py` installs and trusts this hook's handler as a second `apply_patch` group, by the same rules it uses for the fn-length handler: append a new group, never renumber existing groups, trust by `config/batchWrite`. `check` reports both. The command string is byte-identical to the `settings.json` entry.
- **Shared scanner.** Give each name `mul_add_lib.py` imports a public, role-named name in `fn_length_lib.py`: the token type says what it is (for example `RustToken`, not `_Token`). Where the scope readers repeat each other, fold the manifest walk and the lint-level read into one reader parameterized by lint and group. Each hook keeps its own scope type (`SuboptimalFlopsLintScope` and fn-length's). Then drop the ignore; `test_fn_length.py` stays green.
- **no_std, per target.** The folded reader decides no_std from the crate root of the edited file's target:
  - `src/main.rs` and `src/bin/**` belong to binaries;
  - `tests/`, `examples/` and `benches/` files are their own crates;
  - any other file under `src/` belongs to `src/lib.rs` when it exists, else to `src/main.rs`. When both roots exist and only one is no_std, it passes, so the hook never blocks on doubt.

  It recognizes `#![no_std]` with any spacing, and `#![cfg_attr(<condition>, no_std)]` (clippy skips the configuration where the attribute applies). An attribute inside a comment or a string does not count.
- **Patch-path parsing.** Move the fn-length hook's `apply_patch` path parsing into one named function in `fn_length_lib.py` (allowed after G3), and call it from both hooks. Acceptance cases cover Add, Update, Move and Delete file headers.

**Files:**
- `scripts/hooks/post-tool-use-mul-add.py` — `apply_patch` payloads
- `scripts/hooks/post-tool-use-fn-length.py` — calls the shared parser and its payload type
- `scripts/hooks/mul_add_lib.py` — imports the public scanner names; per-target no_std through the shared reader
- `scripts/hooks/fn_length_lib.py` — the shared patch-path parser, payload types, public scanner names and folded scope reader, after G3
- `scripts/hooks/codex_hooks.py` — the second hook
- `scripts/hooks/test_mul_add.py` — Codex payloads, error path, `additionalContext`, no_std per target
- `scripts/hooks/test_fn_length.py` — the shared parser through the fn-length entry
- `scripts/hooks/test_codex_hooks.py` — its tests

**Seats:** 1 writer + 1 tester.
`impl` — `post-tool-use-mul-add.py`, `post-tool-use-fn-length.py`, `fn_length_lib.py`, `mul_add_lib.py`, `codex_hooks.py`.
`test` — `test_mul_add.py`, `test_fn_length.py`, `test_codex_hooks.py`.

**Acceptance gate:**
- **Before the checkpoint:** tests green (`test_mul_add.py`, `test_fn_length.py`, `test_codex_hooks.py`); basedpyright 0/0 on each changed file. Tests cover:
  - Add, Update, Move and Delete headers, and a patch with findings in two files;
  - an internal error, which prints exactly one `mul_add hook error: <type>: <message>` `systemMessage` and exits 0;
  - a block's `additionalContext`, which equals its reasons joined by newlines;
  - no_std per target: a spaced attribute, `cfg_attr`, and a std `main.rs` beside a no_std library.
- **Speed:** measure the per-edit CPU delta as Phase 3 did: 20 interleaved runs, `getrusage` children, the hook against bare Python. Report it for one 236-line real file (at most +23 ms) and for a three-file Codex patch.
- **After the showrunner merges this phase into `~/.claude` main, and the Mac has pulled:** install, then `check`, on natedev (`dangerouslyDisableSandbox`) and on the Mac over `ssh mac` (print `rc=$?` inside). Both must report both hooks trusted and the existing groups unchanged. A `codex exec` smoke in a scratchpad crate (`fn f(x: f32) -> f32 { x * 0.5 + 1.0 }`, `nursery` denied) shows the reason and an `"agent": "codex"` log line. Record T_codex_mul_add in PDT; Phase 3's live Claude smoke records T_claude_mul_add before it.

### Phase 5 — Re-measure: long functions and multiply-adds out of hana's clippy failures · status: todo

#### Work Order

**Blocked by:** G4.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. State every time in PDT.

**Goal:** one table, `too_many_lines` and `suboptimal_flops` side by side, before and after both hooks, with a verdict per lint (user, item 4).

**Constraints from prior phases:**
- **The detector's reach is narrower than "holds a float literal".** A literal proves a product only when the other operand is not excluded. Excluded: a non-float annotation, a pattern or closure binding, a reference, a vector, or a non-float file const. A named field must be declared `f32`/`f64` in the same file. Phase 3 ran it on the 291 historical positions, each inside a stub function, and it flagged 35 (12.0%). That is a lower bound, since a stub drops the file's own declarations. So the 75% threshold for `suboptimal_flops` depends on how far the real files reach.
- The control edit must declare its operand in the same function: `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }`. An untyped `x` is not proved.

**Spec:**
- **Before measuring:** confirm Phase 1's script hashes; a mismatch stops the phase.
- **Measurements:**
  - Run Phase 1's commands for `suboptimal_flops`, and the commands of the fn-length plan's re-measure phase ("Re-measure four days after both hooks are live") for `too_many_lines`.
  - Run both in the same window, between T_codex_mul_add + 96 h and + 100 h. Report a window that includes hours before either lint's T_codex as such.
  - Derive the three numbers per lint. Compare them with Phase 1 here and with the fn-length plan's Phase 1.
- **`suboptimal_flops` baseline, unrounded:**
  - failed steps: 7.916 per 100 (limit ≤ 1.979);
  - sole-cause steps: 2.573 per 100;
  - sole-cause seat time: 0.402 h a day (limit ≤ 0.100).
- **Control:** inside the window, make one controlled Claude edit and one Codex edit, each leaving `fn f(x: f32) -> f32 { x * 0.5 + 1.0 }` in a scratchpad crate that denies `nursery`. The control proves the hooks fire, rather than that no qualifying edit happened. Match residuals on each block record's time, path, line and expression.
- **Success per lint:**
  - failed steps per 100 ≤ 25% of its baseline;
  - sole-cause seat time per day ≤ 25% of its baseline;
  - the control: each hook's `blocks.jsonl` holds at least one `claude` and one `codex` line inside the window, on natedev and the Mac.
- **Residuals for `suboptimal_flops`:** for each diagnostic in the window, record whether a block for that file precedes it. Split them by whether the detector flags the expression: run `float_mul_add_findings` on the file at the commit clippy checked when git has it, else on the snippet inside a stub function (a lower bound). Report the failed steps per 100 the detector reaches beside each global verdict. The 75% thresholds stay as set.
- **FMA in CI:** report whether hana `origin/main`'s `.github/workflows/ci.yml` carries `-C target-cpu=x86-64-v3` (`git -C ~/rust/hana show origin/main:.github/workflows/ci.yml`). Phase 2's real-code check found it only on `init/catalyst`.
- **Write-up:** put the table and verdicts in the As-built, and send natedev the verdict lines, the CI flag's state included.

**Files:**
- `docs/plans/build-followups-mul-add.md` — the As-built.

**Seats:** 1 writer — `impl` runs the commands and reports.

**Acceptance gate:** all hashes match, every command exits 0, and the As-built states for each lint whether each success condition is met, with its number, plus the detector's reach split and the CI flag's state.
