---
description: Reproduce a cargo-mend failure on an external project in a throwaway /tmp copy, diagnose and fix cargo-mend, install after the fix is confirmed, and clean up only after the user verifies
---

# Mend Fix

**Arguments**: `$ARGUMENTS` — path to the project `cargo mend` is failing on. If empty, ask the user for it before doing anything else.

cargo-mend source lives at `/Users/natemccoy/rust/cargo-mend`. All fixes go there, never in the target project.

**The failure is always the same one**: `cargo mend --fix-all` applies rewrites that no longer compile. mend re-checks after applying, the compiler errors, and it prints `compiler failed after applying mend fixes; changes were rolled back` and exits 2. Plain `cargo mend` exits 0 — a clean read-only run is not evidence of anything. Never ask the user which failure they mean; this command is only invoked when `--fix` is broken.

<ExecutionSteps>

**STEP 1 — Resolve the target**
- Expand `$ARGUMENTS` to an absolute path; call it `$SRC`. Verify `$SRC/Cargo.toml` exists.
- If `$SRC` is a workspace member with `path = "../…"` dependencies that reach outside it, use the workspace root as `$SRC` instead — a copy that can't resolve its path deps won't reproduce anything.
- Don't ask what the failure is or which flags to use — go straight to `--fix-all` in STEP 3.

**STEP 2 — Copy to /tmp**
```bash
DEST=/tmp/mend-repro-$(basename "$SRC")
rm -rf "$DEST" && mkdir -p "$DEST"
rsync -a --exclude 'target/' --exclude '.git/' "$SRC"/ "$DEST"/pristine/
rsync -a "$DEST"/pristine/ "$DEST"/work/
```
- `$DEST/pristine` is never run against. `$DEST/work` is the run target.
- To reset `work` after a `--fix` run mutated it, keeping its build cache:
  `rsync -a --delete --exclude 'target/' "$DEST"/pristine/ "$DEST"/work/`
- **Never** run cargo-mend against `$SRC` itself, and especially never with a `--fix` flag.
- If the failure turns out to depend on git state, re-copy without the `.git` exclude.

**STEP 3 — Reproduce with the installed binary**
```bash
cargo mend --fix-all "$DEST/work" > "$DEST"/repro.log 2>&1; echo "EXIT=$?" >> "$DEST"/repro.log
```
- Background it (`run_in_background: true`) — the first run does a full cold `cargo check` of the target project, minutes for anything bevy-sized — and yield the turn.
- Expect `EXIT=2` and the rollback line. The compiler errors in that log are what to diagnose; they come from mend's own post-fix re-check, so a later plain `cargo check` of `work` can still pass (the fixes were rolled back). Don't read that as "it builds fine".
- Record the installed version with `cargo mend --build-info` so the repro is pinned to a known binary.
- If the target's build scripts wrap a macOS framework, the sandbox will fail with `sandbox-exec: sandbox_apply: Operation not permitted`; re-run with `dangerouslyDisableSandbox: true`. That is a sandbox failure, not a cargo-mend bug.
- If it does **not** reproduce, try `--fix`, `--fix-pub-use`, and `--fix-compiler` individually before reporting back — the breakage may be confined to one fix pass.

**STEP 4 — Diagnose in cargo-mend**
- Work in `/Users/natemccoy/rust/cargo-mend`. Prefer LSP (definitions, references, hover) over grep.
- Trace from the observed symptom to the code that produced it before changing anything. Name the mechanism, not a guess.
- If two attempts fail to resolve it, start an attempts log in the cargo-mend memory directory and log every approach, reasoning, and result before the next attempt — telling the user each time an entry is added.

**STEP 5 — Fix**
- Run `/rust_style` immediately before editing Rust.
- Make the fix. Add or extend a regression test under `tests/` when the bug is expressible there.
- `cargo build && cargo +nightly fmt` (background the build).

**STEP 6 — Confirm the fix**
- Build the dev binary with the stable toolchain and run it against the copy:
```bash
cargo +stable build            # RUSTC_BOOTSTRAP=1 comes from .cargo/config.toml
rsync -a --delete --exclude 'target/' "$DEST"/pristine/ "$DEST"/work/   # fresh sources, keep build cache
./target/debug/cargo-mend --fix-all "$DEST/work"
```
  `+stable` is load-bearing — a nightly-built binary fails against stable projects with `E0514`, which reads like a fresh bug.
- The fix is confirmed only when mend exits 0 **and** the rewritten copy compiles: `cargo check --workspace --all-targets` in `$DEST/work` after the fix run.
- Run `cargo test` (background it).
- Report the before/after to the user: the original compiler errors, and what the run does now.

**STEP 7 — Install**
Only after the fix is confirmed and tests pass. Run this exactly, without asking:
```bash
RUSTC_BOOTSTRAP=1 cargo +stable install --path .
```

**STEP 8 — Hand off for user verification, then stop**
- Tell the user to run `cargo mend --fix-all` in the real project at `$SRC` themselves.
- **Do not delete `$DEST`.** Do not commit unless the user asks.
- End the turn there and wait.

**STEP 9 — Clean up after the user confirms**
- On confirmation: `rm -rf "$DEST"` and say it's gone.
- If the user reports it still fails, go back to STEP 3 with their new output — the copy is still there for exactly this.

</ExecutionSteps>
