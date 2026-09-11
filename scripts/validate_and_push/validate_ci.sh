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

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - When the repo builds cargo-mend, fix and strict steps invoke that build
#   instead of the installed binary, so a mend change gates its own push; all
#   other repos use the installed cargo-mend through LINT_CMD
# - Clippy lints --all-targets, the same scope CI uses; benches are linted but
#   intentionally never run here — run them ad hoc
# - `mend=off` in config/lint.conf skips both cargo-mend steps here, and only
#   those two. Every other step ignores that file: a pre-push gate that silently
#   no-ops is worse than a noisy one. mend is the exception because it rewrites
#   source, so a mend release that emits a fix which does not compile blocks
#   every push from an affected repo with nothing the repo can do about it

worktree_has_changes() {
  ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]
}

# Abort if the worktree is dirty (staged, unstaged, or untracked files).
if worktree_has_changes; then
  echo "!!! Cannot validate — there are uncommitted changes. Please commit or discard them first."
  exit 1
fi

TAB="$(printf '\t')"

# Workspace members as "name<TAB>manifest-path", resolved once. `cargo metadata
# --no-deps` reports members only, which is the scope the cargo-mend decision
# below needs. An empty result means metadata or jq is
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

run_step "clippy" env LINT_CONFIG_FORCE=1 "$LINT_CMD" clippy --workspace

# rustdoc across every member. `lint doc` scopes to changed members like the
# other checks, so the dev-time run cannot see a doc link that rotted in an
# untouched crate when a public item was renamed elsewhere. This is the sweep
# that catches it, and the only place cargo doc runs workspace-wide.
run_step "rustdoc" env LINT_CONFIG_FORCE=1 "$LINT_CMD" doc --workspace

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
