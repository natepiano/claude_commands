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
# - In the cargo-mend repo, final step uses `cargo run -- --fail-on-warn`
# - In all other repos, final step uses installed `cargo mend`
# - If `benches/` exists, Criterion benches are run as a smoke check
# - If `.cargo/validate-targets` exists, each non-comment target listed there
#   gets additive cross-target clippy plus test-binary compilation. Host checks
#   still run, so this validates macOS plus configured Linux targets on a Mac.

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
    case "$target" in
      x86_64-unknown-linux-gnu)
        ensure_linux_cross_env
        run_step "clippy ${target}" env \
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
          "$LINT_CMD" clippy --target "$target"
        run_step "compile tests ${target}" env \
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
        run_step "clippy ${target}" "$LINT_CMD" clippy --target "$target"
        run_step "compile tests ${target}" cargo test --target "$target" --workspace --all-features --tests --no-run
        ;;
    esac
  done < "$targets_file"
}

run_step "rustfmt" "$LINT_CMD" fmt --check

run_step "taplo" taplo fmt --check

run_step "clippy" "$LINT_CMD" clippy

run_configured_target_checks

run_step "check examples" cargo check --workspace --all-features --examples

run_step "nextest" cargo nextest run --workspace --all-features --tests

if [ -d benches ] && find benches -type f \( -name '*.rs' -o -name '*.bench' \) | grep -q .; then
  run_step "bench" cargo bench --workspace --all-features
fi

if [ "$IS_SELF_MEND" -eq 1 ]; then
  run_step "cargo-mend" cargo run -- --fail-on-warn
else
  run_step "cargo-mend" "$LINT_CMD" mend --fail-on-warn
fi

echo ""
echo "=== ALL VALIDATION STEPS PASSED ==="
