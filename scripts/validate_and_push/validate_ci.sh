#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - In the cargo-mend repo, final step uses `cargo run -- --fail-on-warn`
# - In all other repos, final step uses installed `cargo mend`
# - If `benches/` exists, Criterion benches are run as a smoke check
# - If `.cargo/validate-targets` exists, each non-comment target listed there
#   gets cross-target clippy plus test-binary compilation

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

trim_target_line() {
  local target="$1"
  target="${target%%#*}"
  target="${target#"${target%%[![:space:]]*}"}"
  target="${target%"${target##*[![:space:]]}"}"
  printf '%s' "$target"
}

run_configured_target_checks() {
  local targets_file=".cargo/validate-targets"
  if [ ! -f "$targets_file" ]; then
    return 0
  fi

  local target
  while IFS= read -r target || [ -n "$target" ]; do
    target="$(trim_target_line "$target")"
    if [ -z "$target" ]; then
      continue
    fi
    case "$target" in
      x86_64-unknown-linux-gnu)
        run_step "clippy ${target}" env \
          CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="${SCRIPT_DIR}/zig-linux-cc" \
          AR_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-ar" \
          CC_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cc" \
          CXX_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cxx" \
          cargo clippy --workspace --target "$target" --all-targets --all-features -- -D warnings
        run_step "compile tests ${target}" env \
          CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="${SCRIPT_DIR}/zig-linux-cc" \
          AR_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-ar" \
          CC_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cc" \
          CXX_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cxx" \
          cargo test --target "$target" --workspace --all-features --tests --no-run
        ;;
      *)
        run_step "clippy ${target}" cargo clippy --workspace --target "$target" --all-targets --all-features -- -D warnings
        run_step "compile tests ${target}" cargo test --target "$target" --workspace --all-features --tests --no-run
        ;;
    esac
  done < "$targets_file"
}

run_step "rustfmt" cargo +nightly fmt --all --check

run_step "taplo" taplo fmt --check

run_step "clippy" cargo clippy --workspace --all-targets --all-features -- -D warnings

run_configured_target_checks

run_step "check examples" cargo check --workspace --all-features --examples

run_step "nextest" cargo nextest run --workspace --all-features --tests

if [ -d benches ] && find benches -type f \( -name '*.rs' -o -name '*.bench' \) | grep -q .; then
  run_step "bench" cargo bench --workspace --all-features
fi

if [ "$IS_SELF_MEND" -eq 1 ]; then
  run_step "cargo-mend" cargo run -- --fail-on-warn
else
  run_step "cargo-mend" cargo mend --workspace --all-targets --fail-on-warn
fi

echo ""
echo "=== ALL VALIDATION STEPS PASSED ==="
