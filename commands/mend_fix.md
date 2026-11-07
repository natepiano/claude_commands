---
description: Reproduce and fix a cargo-mend failure.
---

# Mend Fix

**Arguments**: `$ARGUMENTS` — path to the project `cargo mend` is failing on. If empty, ask the user for it before doing anything else.

cargo-mend source lives at `~/rust/cargo-liner/crates/cargo-mend`, a member of the cargo-liner workspace. All fixes go there, never in the target project.

**The failure is always the same one**: `cargo mend --fix-all` applies rewrites that no longer compile. mend re-checks after applying, the compiler errors, and it prints `compiler failed after applying mend fixes; changes were rolled back` and exits 2. Plain `cargo mend` exits 0 — a clean read-only run is not evidence of anything. Never ask the user which failure they mean; this command is only invoked when `--fix` is broken.

<ExecutionSteps>

**STEP 1 — Resolve the target**
- Expand `$ARGUMENTS` to an absolute path; call it `$SRC`. Verify `$SRC/Cargo.toml` exists.
- If `$SRC` is a workspace member with `path = "../…"` dependencies that reach outside it, use the workspace root as `$SRC` instead — a copy that can't resolve its path deps won't reproduce anything.
- Don't ask what the failure is or which flags to use — go straight to `--fix-all` in STEP 3.

**STEP 2 — Select the reproduction**
- Resolve the owner and session scratchpad. In every command below, replace `/absolute/project`, `/absolute/repository`, `/absolute/session-scratch`, `mend-fix`, and `relative/project` with the concrete paths, owner and workspace-relative path before issuing a tool call. Do not combine discovery and execution in a shell branch or command array.
- First discover whether the selected project is in Git (a nonzero exit selects the non-git route):
```bash
git -C /absolute/project rev-parse --show-toplevel
```
- Git route: use that repository root; calculate the selected workspace's relative path separately. Each attempt uses `copies.py run`, resets sources and keeps its build cache.
- Non-git route only: prepare an immutable pristine directory and a working reproduction under the scratchpad. `git init` lets mend's `cargo fix` run; it creates no repository copy:
```bash
mkdir -p /absolute/session-scratch/pristine /absolute/session-scratch/work
rsync -a --exclude 'target/' --exclude '.git' /absolute/project/ /absolute/session-scratch/pristine/
rsync -a /absolute/session-scratch/pristine/ /absolute/session-scratch/work/
git -C /absolute/session-scratch/work init -q .
git -C /absolute/session-scratch/work add -A
git -C /absolute/session-scratch/work -c user.email=t@t -c user.name=t commit -qm base
```
- Reset a non-git attempt from pristine with `rsync -a --delete --exclude 'target/' --exclude '.git' /absolute/session-scratch/pristine/ /absolute/session-scratch/work/`, after substituting the concrete scratchpad. Never apply fix flags to the original project.

**STEP 3 — Reproduce with the installed binary**
- Issue exactly the selected route, with concrete substitutions from STEP 2.
Git route:
```bash
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --owner mend-fix --repo /absolute/repository --commit HEAD --apply-uncommitted /absolute/repository -- cargo mend --fix-all '{tree}/relative/project' > /absolute/session-scratch/repro.log 2>&1
echo "EXIT=$?" >> /absolute/session-scratch/repro.log
```
Non-git route:
```bash
cargo mend --fix-all /absolute/session-scratch/work > /absolute/session-scratch/repro.log 2>&1
echo "EXIT=$?" >> /absolute/session-scratch/repro.log
```
- Background it (`run_in_background: true`) — the first run does a full cold `cargo check` of the target project, minutes for anything bevy-sized — and yield the turn.
- Expect `EXIT=2` and the rollback line. The compiler errors in that log are what to diagnose; they come from mend's own post-fix re-check, so a later plain `cargo check` of the reset checkout can still pass (the fixes were rolled back). Don't read that as "it builds fine".
- Record the installed version with `cargo mend --build-info` so the repro is pinned to a known binary.
- If the target's build scripts wrap a macOS framework, the sandbox will fail with `sandbox-exec: sandbox_apply: Operation not permitted`; re-run with `dangerouslyDisableSandbox: true`. That is a sandbox failure, not a cargo-mend bug.
- If it does **not** reproduce, try `--fix`, `--fix-pub-use`, and `--fix-compiler` individually before reporting back — the breakage may be confined to one fix pass.

**STEP 4 — Diagnose in cargo-mend**
- Work in `~/rust/cargo-liner/crates/cargo-mend`. Prefer LSP (definitions, references, hover) over grep.
- Trace from the observed symptom to the code that produced it before changing anything. Name the function and the value it computed.
- Isolate the pattern in a dependency-free fixture crate under `$DEST` and confirm it reproduces there. A 6-file crate that fails in one second beats re-checking a bevy workspace, and it becomes the regression test.
- If two attempts fail to resolve it, start an attempts log in the cargo-mend memory directory and log every approach, reasoning, and result before the next attempt — telling the user each time an entry is added.

**STEP 5 — Fix**
- This session has no style reviewer; follow `/rust_style` before editing Rust.
- Make the fix. Add or extend a regression test under `tests/` when the bug is expressible there.
- `cargo build && cargo +nightly fmt` (background the build).

**STEP 6 — Confirm the fix**
- Build the dev binary with `cargo +stable build` (`RUSTC_BOOTSTRAP=1` comes from `.cargo/config.toml`). Reset the selected reproduction and run the corresponding concrete command below. Replace `/absolute/cargo-liner` with its real path too.
Git route:
```bash
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --owner mend-fix --repo /absolute/repository --commit HEAD --apply-uncommitted /absolute/repository -- sh -c '/absolute/cargo-liner/target/debug/cargo-mend --fix-all "{tree}/relative/project" && cd "{tree}/relative/project" && cargo check --workspace --all-targets'
```
Non-git route:
```bash
/absolute/cargo-liner/target/debug/cargo-mend --fix-all /absolute/session-scratch/work && cd /absolute/session-scratch/work && cargo check --workspace --all-targets
```
  `+stable` matters — a nightly-built binary fails against stable projects with `E0514`, which reads like a fresh bug.
- The fix is confirmed only when mend exits 0 **and** the rewritten copy compiles: `cargo check --workspace --all-targets` in the same reproduction after the fix run.
- Run `cargo nextest run` (background it).
- Report the before/after to the user: the original compiler errors, and what the run does now.

**STEP 7 — Install**
Only after the fix is confirmed and tests pass. Run this exactly, without asking:
```bash
RUSTC_BOOTSTRAP=1 cargo +stable install --path ~/rust/cargo-liner/crates/cargo-mend
```

**STEP 8 — Hand off for user verification, then stop**
- Tell the user to run `cargo mend --fix-all` in the real project at `$SRC` themselves.
- **Do not delete `$DEST`.** Do not commit unless the user asks.
- End the turn there and wait.

**STEP 9 — Clean up after the user confirms**
- On confirmation: `rm -rf "$DEST"` and say it's gone.
- If the user reports it still fails, go back to STEP 3 with their new output — reset the selected reproduction before the next attempt.

</ExecutionSteps>
