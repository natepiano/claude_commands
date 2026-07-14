#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LINT_CMD="$HOME/.claude/scripts/clippy/lint"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REPO_TARGET_DIR="${CARGO_TARGET_DIR:-${REPO_ROOT}/target}"
export CARGO_TARGET_DIR="$REPO_TARGET_DIR"
export VALIDATE_TARGET_DIR="$REPO_TARGET_DIR"
LINUX_SYSROOT_PREPARED=0
HOST_TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - In the cargo-mend repo, fix and strict steps invoke the local binary through
#   `cargo run`; all other repos use the installed cargo-mend through LINT_CMD
# - Host clippy lints lib/bins/tests only (examples and benches excluded);
#   benches are intentionally never run here — run them ad hoc
# - If `.cargo/validate-targets` exists, each non-comment target listed there
#   gets additive cross-target clippy plus test-binary compilation. Host checks
#   still run, so this validates macOS plus configured Linux targets on a Mac.

worktree_has_changes() {
  ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]
}

# Abort if the worktree is dirty (staged, unstaged, or untracked files).
if worktree_has_changes; then
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

amend_fixes() {
  local label="$1"
  if ! worktree_has_changes; then
    return 0
  fi

  echo "=== STEP: amend ${label} fixes ==="
  git add -A
  git commit --amend --no-edit --quiet
  echo "Amended ${label} fixes into the last commit; continuing validation."
}

run_autofix_step() {
  local label="$1"
  shift
  run_step "$label" "$@"
  amend_fixes "$label"
}

repo_uses_rustc_private() {
  if [ "${VALIDATE_FORCE_CROSS_TARGETS:-0}" = "1" ]; then
    return 1
  fi

  local search_roots=()
  [ -d src ] && search_roots+=(src)
  [ -d tests ] && search_roots+=(tests)
  [ -d benches ] && search_roots+=(benches)
  [ -d examples ] && search_roots+=(examples)
  [ "${#search_roots[@]}" -gt 0 ] || return 1

  grep -R -E '(^|[^[:alnum:]_])rustc_(driver|hir|interface|middle|span)(::|[[:space:]]|$)' \
    "${search_roots[@]}" >/dev/null 2>&1
}

skip_unsupported_cross_target() {
  local target="$1"
  if [ "$target" = "$HOST_TRIPLE" ]; then
    return 1
  fi

  if repo_uses_rustc_private; then
    echo "=== STEP: skip ${target} ==="
    echo "Skipping configured cross-target ${target}: this repo uses rustc_private crates, which are tied to the rustc host toolchain."
    echo "Host validation still runs here; run validation on a Linux host or rely on Linux CI for native Linux coverage."
    echo "Set VALIDATE_FORCE_CROSS_TARGETS=1 to force the cross-target check."
    return 0
  fi

  return 1
}

ensure_linux_cross_env() {
  if [ "$LINUX_SYSROOT_PREPARED" -eq 1 ]; then
    return 0
  fi

  local linux_sysroot="${VALIDATE_LINUX_SYSROOT:-${REPO_TARGET_DIR}/validate-linux-sysroot}"
  VALIDATE_LINUX_SYSROOT="$linux_sysroot" bash "${SCRIPT_DIR}/ensure_linux_sysroot.sh"
  # shellcheck disable=SC1091
  source "${linux_sysroot}/env.sh"
  LINUX_SYSROOT_PREPARED=1
}

trim_target_line() {
  local target="$1"
  target="${target%%#*}"
  target="${target#"${target%%[![:space:]]*}"}"
  target="${target%"${target##*[![:space:]]}"}"
  printf '%s' "$target"
}

run_target_clippy() {
  local target="$1"
  shift

  case "$target" in
    x86_64-unknown-linux-gnu)
      ensure_linux_cross_env
      env \
        CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="${SCRIPT_DIR}/zig-linux-cc" \
        AR_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-ar" \
        CC_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cc" \
        CXX_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cxx" \
        VALIDATE_TARGET_DIR="${REPO_TARGET_DIR}" \
        CARGO_TARGET_DIR="${REPO_TARGET_DIR}" \
        VALIDATE_LINUX_SYSROOT="${VALIDATE_LINUX_SYSROOT}" \
        PKG_CONFIG_SYSROOT_DIR="${PKG_CONFIG_SYSROOT_DIR}" \
        PKG_CONFIG_LIBDIR="${PKG_CONFIG_LIBDIR}" \
        PKG_CONFIG_PATH= \
        PKG_CONFIG_ALLOW_CROSS=1 \
        PKG_CONFIG_ALLOW_CROSS_x86_64_unknown_linux_gnu=1 \
        "$LINT_CMD" clippy --target "$target" "$@"
      ;;
    *)
      "$LINT_CMD" clippy --target "$target" "$@"
      ;;
  esac
}

compile_target_tests() {
  local target="$1"

  case "$target" in
    x86_64-unknown-linux-gnu)
      ensure_linux_cross_env
      env \
        CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="${SCRIPT_DIR}/zig-linux-cc" \
        AR_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-ar" \
        CC_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cc" \
        CXX_x86_64_unknown_linux_gnu="${SCRIPT_DIR}/zig-linux-cxx" \
        VALIDATE_TARGET_DIR="${REPO_TARGET_DIR}" \
        CARGO_TARGET_DIR="${REPO_TARGET_DIR}" \
        VALIDATE_LINUX_SYSROOT="${VALIDATE_LINUX_SYSROOT}" \
        PKG_CONFIG_SYSROOT_DIR="${PKG_CONFIG_SYSROOT_DIR}" \
        PKG_CONFIG_LIBDIR="${PKG_CONFIG_LIBDIR}" \
        PKG_CONFIG_PATH= \
        PKG_CONFIG_ALLOW_CROSS=1 \
        PKG_CONFIG_ALLOW_CROSS_x86_64_unknown_linux_gnu=1 \
        cargo test --target "$target" --workspace --all-features --tests --no-run
      ;;
    *)
      cargo test --target "$target" --workspace --all-features --tests --no-run
      ;;
  esac
}

run_configured_target_autofixes() {
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
    if skip_unsupported_cross_target "$target"; then
      continue
    fi
    run_autofix_step "clippy autofix ${target}" run_target_clippy "$target" \
      --fix --allow-dirty --allow-staged --jobs 1 -- --cap-lints warn
  done < "$targets_file"
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
    if skip_unsupported_cross_target "$target"; then
      continue
    fi
    run_step "clippy ${target}" run_target_clippy "$target"
    run_step "compile tests ${target}" compile_target_tests "$target"
  done < "$targets_file"
}

if [ "$IS_SELF_MEND" -eq 1 ]; then
  run_autofix_step "cargo-mend autofix" cargo run -- --fix
else
  run_autofix_step "cargo-mend autofix" "$LINT_CMD" mend --fix
fi

# Cap lint severity during fix passes so clippy can apply every available
# machine-applicable suggestion. Strict checks below reject anything remaining.
run_autofix_step "clippy autofix" cargo clippy --fix --allow-dirty --allow-staged \
  --jobs 1 --workspace --all-features --tests -- --cap-lints warn

run_configured_target_autofixes

run_autofix_step "rustfmt" "$LINT_CMD" fmt

run_autofix_step "taplo" taplo fmt

run_step "clippy" cargo clippy --workspace --all-features --tests -- -D warnings

run_configured_target_checks

run_step "nextest" cargo nextest run --workspace --all-features --tests

if [ "$IS_SELF_MEND" -eq 1 ]; then
  run_step "cargo-mend" cargo run -- --fail-on-warn
else
  run_step "cargo-mend" "$LINT_CMD" mend --fail-on-warn
fi

echo ""
echo "=== ALL VALIDATION STEPS PASSED ==="
