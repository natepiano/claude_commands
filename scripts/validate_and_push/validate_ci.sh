#!/usr/bin/env bash
set -euo pipefail

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - In the cargo-mend repo, final step uses `cargo run -- --fail-on-warn`
# - In all other repos, final step uses installed `cargo mend`
# - If `benches/` exists, Criterion benches are run as a smoke check

# Abort if the worktree is dirty (staged, unstaged, or untracked files).
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "!!! Cannot validate — there are uncommitted changes. Please commit or discard them first."
  exit 1
fi

REPO_NAME="$(basename "$PWD")"
IS_SELF_MEND=0
if [ "$REPO_NAME" = "cargo-mend" ]; then
  IS_SELF_MEND=1
fi

run_step() {
  local label="$1"
  shift
  echo "=== STEP: ${label} ==="
  if ! "$@"; then
    echo ""
    echo "!!! VALIDATION FAILED at step: ${label} !!!"
    echo "!!! Command: $* !!!"
    exit 1
  fi
}

run_step "rustfmt" cargo +nightly fmt --all --check

run_step "taplo" taplo fmt --check

run_step "clippy" cargo clippy --workspace --all-targets --all-features -- -D warnings

run_step "build examples" cargo build --workspace --all-features --examples

run_step "nextest" cargo nextest run --workspace --all-features --tests

if [ -d benches ] && find benches -type f \( -name '*.rs' -o -name '*.bench' \) | grep -q .; then
  run_step "bench" cargo bench --workspace --all-features
fi

if [ "$IS_SELF_MEND" -eq 1 ]; then
  run_step "cargo-mend" cargo run -- --fail-on-warn
else
  run_step "cargo-mend" cargo mend --fail-on-warn
fi

echo ""
echo "=== ALL VALIDATION STEPS PASSED ==="
