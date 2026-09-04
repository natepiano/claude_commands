#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Single bottom layer for cargo invocations: scripts/lint owns every command's
# flags (lint CLI over invoke.sh); this script only picks subcommands.
# LINT_CONFIG_FORCE=1 is set on each step that must never be skipped by
# config/lint.conf — as a pre-push gate, only the two mend steps honor that
# file (see the mend note below).
# Every lint call here passes --workspace explicitly. The lint CLI otherwise
# narrows scope to the members the working tree changed, which is right for a
# dev loop and wrong for a pre-push gate: a change that compiles in its own
# crate can still break a dependent one, and this is the last check before the
# push. Full coverage is the point.
LINT_CMD="$HOME/.claude/scripts/lint/lint"

# Lint policy, read for `mend` only — see the mend note in the header comment
# below. A missing reader means every step runs.
LINT_CONFIG_READER="$HOME/.claude/scripts/lint/lint_config.sh"
if [ -f "$LINT_CONFIG_READER" ]; then
  # shellcheck source=/dev/null
  source "$LINT_CONFIG_READER"
else
  echo "validate_ci.sh: $LINT_CONFIG_READER not found — running every step" >&2
  lint_config_enabled() { return 0; }
  lint_config_skip_notice() { :; }
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REPO_TARGET_DIR="${CARGO_TARGET_DIR:-${REPO_ROOT}/target}"
export CARGO_TARGET_DIR="$REPO_TARGET_DIR"
export VALIDATE_TARGET_DIR="$REPO_TARGET_DIR"
LINUX_SYSROOT_PREPARED=0
HOST_TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - When the repo builds cargo-mend, fix and strict steps invoke that build
#   instead of the installed binary, so a mend change gates its own push; all
#   other repos use the installed cargo-mend through LINT_CMD
# - Host clippy lints lib/bins/tests only (examples and benches excluded);
#   benches are intentionally never run here — run them ad hoc
# - `mend=off` in config/lint.conf skips both cargo-mend steps here, and only
#   those two. Every other step ignores that file: a pre-push gate that silently
#   no-ops is worse than a noisy one. mend is the exception because it rewrites
#   source, so a mend release that emits a fix which does not compile blocks
#   every push from an affected repo with nothing the repo can do about it
# - If `.cargo/validate-targets` exists, each non-comment target listed there
#   gets additive cross-target clippy plus test-binary compilation. Host checks
#   still run, so this validates macOS plus configured Linux targets on a Mac.
#   Packages using rustc_private are excluded from those cross-target steps one
#   by one — rustc-dev is host-only — and a target is skipped outright only when
#   no package is left to check

worktree_has_changes() {
  ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]
}

# Abort if the worktree is dirty (staged, unstaged, or untracked files).
if worktree_has_changes; then
  echo "!!! Cannot validate — there are uncommitted changes. Please commit or discard them first."
  exit 1
fi

TAB="$(printf '\t')"
RUSTC_PRIVATE_MARKERS='(^|[^[:alnum:]_])rustc_(driver|hir|interface|middle|span)(::|[[:space:]]|$)'

# Workspace members as "name<TAB>manifest-path", resolved once. `cargo metadata
# --no-deps` reports members only, which is the scope both the cross-target and
# the cargo-mend decisions below need. An empty result means metadata or jq is
# unavailable; each caller then falls back to a repo-wide test rather than
# guessing at package boundaries.
WORKSPACE_MEMBERS=""
WORKSPACE_MEMBERS_RESOLVED=0

workspace_members() {
  if [ "$WORKSPACE_MEMBERS_RESOLVED" -eq 0 ]; then
    WORKSPACE_MEMBERS_RESOLVED=1
    if command -v jq >/dev/null 2>&1; then
      WORKSPACE_MEMBERS="$(
        cargo metadata --no-deps --format-version 1 2>/dev/null |
          jq -r '.packages[] | "\(.name)\t\(.manifest_path)"' 2>/dev/null || printf ''
      )"
    fi
  fi
  printf '%s' "$WORKSPACE_MEMBERS"
}

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

# Whether one package's sources use rustc_private. Asked per package, not per
# repo: in a workspace, one rustc_private crate must not disqualify its stable
# siblings from cross-target checks.
package_uses_rustc_private() {
  local pkg_dir="$1"
  local pathspec

  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # `:/` anchors the pathspec at the repo root: git pathspecs are otherwise
    # relative to the caller's directory, where a package path would match
    # nothing and read as "no rustc_private".
    if [ "$pkg_dir" = "$REPO_ROOT" ]; then
      pathspec=':/*.rs'
    else
      pathspec=":/${pkg_dir#"${REPO_ROOT}/"}/*.rs"
    fi
    if git grep -q -E "$RUSTC_PRIVATE_MARKERS" -- "$pathspec" 2>/dev/null; then
      return 0
    fi
    return 1
  fi

  local search_roots=()
  [ -d "$pkg_dir/src" ] && search_roots+=("$pkg_dir/src")
  [ -d "$pkg_dir/tests" ] && search_roots+=("$pkg_dir/tests")
  [ -d "$pkg_dir/benches" ] && search_roots+=("$pkg_dir/benches")
  [ -d "$pkg_dir/examples" ] && search_roots+=("$pkg_dir/examples")
  [ -f "$pkg_dir/build.rs" ] && search_roots+=("$pkg_dir/build.rs")
  [ "${#search_roots[@]}" -gt 0 ] || return 1

  if grep -R -E "$RUSTC_PRIVATE_MARKERS" "${search_roots[@]}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Fallback for repos whose package list could not be read: the original
# repo-wide question, over tracked sources.
repo_uses_rustc_private() {
  package_uses_rustc_private "$REPO_ROOT"
}

# Which workspace packages can be cross-compiled, resolved once. rustc_private
# binds a package to the host rustc toolchain (rustc-dev ships host-only), so
# those packages are excluded by name and the target is skipped only when
# nothing else is left to check.
CROSS_SCOPE_RESOLVED=0
CROSS_SCOPE=""
CROSS_EXCLUDE_ARGS=()
CROSS_EXCLUDED_NAMES=""

resolve_cross_target_scope() {
  if [ "$CROSS_SCOPE_RESOLVED" -eq 1 ]; then
    return 0
  fi
  CROSS_SCOPE_RESOLVED=1

  if [ "${VALIDATE_FORCE_CROSS_TARGETS:-0}" = "1" ]; then
    CROSS_SCOPE="all"
    return 0
  fi

  local members
  members="$(workspace_members)"
  if [ -z "$members" ]; then
    if repo_uses_rustc_private; then
      CROSS_SCOPE="none"
    else
      CROSS_SCOPE="all"
    fi
    return 0
  fi

  local total=0 excluded=0 name manifest pkg_dir
  while IFS="$TAB" read -r name manifest; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    pkg_dir="$(dirname "$manifest")"
    if package_uses_rustc_private "$pkg_dir"; then
      excluded=$((excluded + 1))
      CROSS_EXCLUDE_ARGS+=(--exclude "$name")
      CROSS_EXCLUDED_NAMES="${CROSS_EXCLUDED_NAMES:+${CROSS_EXCLUDED_NAMES}, }${name}"
    fi
  done <<EOF
$members
EOF

  if [ "$excluded" -eq 0 ]; then
    CROSS_SCOPE="all"
  elif [ "$excluded" -ge "$total" ]; then
    CROSS_SCOPE="none"
  else
    CROSS_SCOPE="partial"
  fi
}

skip_unsupported_cross_target() {
  local target="$1"
  if [ "$target" = "$HOST_TRIPLE" ]; then
    return 1
  fi

  resolve_cross_target_scope

  case "$CROSS_SCOPE" in
    none)
      echo "=== STEP: skip ${target} ==="
      echo "Skipping configured cross-target ${target}: every workspace package uses rustc_private crates, which are tied to the rustc host toolchain."
      echo "Host validation still runs here; run validation on a Linux host or rely on Linux CI for native Linux coverage."
      echo "Set VALIDATE_FORCE_CROSS_TARGETS=1 to force the cross-target check."
      return 0
      ;;
    partial)
      echo "Cross-target ${target} excludes rustc_private packages (${CROSS_EXCLUDED_NAMES}): they are bound to the host rustc toolchain and are covered by Linux CI. Every other workspace package is checked."
      ;;
  esac

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

# The x86_64-unknown-linux-gnu arms below link through zig against a downloaded
# sysroot, which is what that target needs FROM macOS, where it is a cross
# build. On a Linux host it is the NATIVE target: cargo links it with the system
# cc, and forcing the cross path there fails at every build script with
# "zig-linux-cc: line 33: exec: zig: not found". Keyed on host != target rather
# than on `uname` so the Mac's cross path is untouched.
cross_kind() {
  if [ "$1" = "$HOST_TRIPLE" ]; then
    printf 'native'
  else
    printf 'cross-%s' "$1"
  fi
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

  case "$(cross_kind "$target")" in
    cross-x86_64-unknown-linux-gnu)
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
        LINT_CONFIG_FORCE=1 \
        "$LINT_CMD" clippy --workspace --target "$target" \
        ${CROSS_EXCLUDE_ARGS[@]+"${CROSS_EXCLUDE_ARGS[@]}"} "$@"
      ;;
    *)
      env LINT_CONFIG_FORCE=1 "$LINT_CMD" clippy --workspace --target "$target" \
        ${CROSS_EXCLUDE_ARGS[@]+"${CROSS_EXCLUDE_ARGS[@]}"} "$@"
      ;;
  esac
}

compile_target_tests() {
  local target="$1"

  case "$(cross_kind "$target")" in
    cross-x86_64-unknown-linux-gnu)
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
        cargo test --target "$target" --workspace --all-features --tests --no-run \
        ${CROSS_EXCLUDE_ARGS[@]+"${CROSS_EXCLUDE_ARGS[@]}"}
      ;;
    *)
      cargo test --target "$target" --workspace --all-features --tests --no-run \
        ${CROSS_EXCLUDE_ARGS[@]+"${CROSS_EXCLUDE_ARGS[@]}"}
      ;;
  esac
}

# The repo builds cargo-mend when a workspace member is named cargo-mend —
# true both for the standalone repo and for a workspace that holds it. Keyed on
# the package rather than the directory name so validation lints with the build
# under test instead of whatever binary was last installed.
MEND_SELF_RESOLVED=0
MEND_SELF_PACKAGE=""

resolve_mend_self_package() {
  if [ "$MEND_SELF_RESOLVED" -eq 1 ]; then
    return 0
  fi
  MEND_SELF_RESOLVED=1

  local members name manifest
  members="$(workspace_members)"
  if [ -n "$members" ]; then
    while IFS="$TAB" read -r name manifest; do
      if [ "$name" = "cargo-mend" ]; then
        MEND_SELF_PACKAGE="$name"
        break
      fi
    done <<EOF
$members
EOF
    return 0
  fi

  if [ "$(basename "$PWD")" = "cargo-mend" ]; then
    MEND_SELF_PACKAGE="cargo-mend"
  fi
}

# Build cargo-mend with the ambient env so its fingerprint matches every other
# build here, then run it with RUSTC_WRAPPER cleared: a compiler cache replays
# cached output instead of running rustc, which suppresses the diagnostics mend
# analyzes.
run_self_mend() {
  local mend_bin="${REPO_TARGET_DIR}/debug/cargo-mend"

  echo "+ cargo build -p ${MEND_SELF_PACKAGE} --bin cargo-mend"
  cargo build -p "$MEND_SELF_PACKAGE" --bin cargo-mend

  if [ ! -x "$mend_bin" ]; then
    echo "validate_ci.sh: no cargo-mend binary at ${mend_bin} after building ${MEND_SELF_PACKAGE}" >&2
    return 1
  fi

  echo "+ env RUSTC_WRAPPER= ${mend_bin} --workspace --all-targets $*"
  env RUSTC_WRAPPER= "$mend_bin" --workspace --all-targets "$@"
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

resolve_mend_self_package

if ! lint_config_enabled mend; then
  lint_config_skip_notice mend "cargo-mend autofix"
elif [ -n "$MEND_SELF_PACKAGE" ]; then
  run_autofix_step "cargo-mend autofix (in-repo build)" run_self_mend --fix
else
  run_autofix_step "cargo-mend autofix" "$LINT_CMD" mend --workspace --fix
fi

run_autofix_step "rustfmt" env LINT_CONFIG_FORCE=1 "$LINT_CMD" fmt

run_autofix_step "taplo" taplo fmt

run_step "clippy" env LINT_CONFIG_FORCE=1 "$LINT_CMD" clippy-tests --workspace

# rustdoc across every member. `lint doc` scopes to changed members like the
# other checks, so the dev-time run cannot see a doc link that rotted in an
# untouched crate when a public item was renamed elsewhere. This is the sweep
# that catches it, and the only place cargo doc runs workspace-wide.
run_step "rustdoc" env LINT_CONFIG_FORCE=1 "$LINT_CMD" doc --workspace

run_configured_target_checks

run_step "nextest" "$LINT_CMD" nextest --workspace --all-features --tests

if ! lint_config_enabled mend; then
  lint_config_skip_notice mend "cargo-mend --fail-on-warn"
elif [ -n "$MEND_SELF_PACKAGE" ]; then
  run_step "cargo-mend (in-repo build)" run_self_mend --fail-on-warn
else
  run_step "cargo-mend" "$LINT_CMD" mend --workspace --fail-on-warn
fi

echo ""
echo "=== ALL VALIDATION STEPS PASSED ==="
