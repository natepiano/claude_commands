#!/usr/bin/env bash
# style_branch.sh — Resolve the commit the project-end style review diffs from,
# and create the project branch when the user approves one.
#
# Usage:
#   style_branch.sh resolve <working-dir> [plan-slug]
#   style_branch.sh create  <working-dir> <branch-name>
#
# `resolve` never writes to the repository. It prints key=value lines:
#
#   status=ok|not_a_repo|error
#   repo_root=<path>
#   current_branch=<name>          empty when HEAD is detached
#   default_branch=<name>          empty when neither origin/HEAD, main, nor master exists
#   default_ref=<ref>              ref merge-base and the log scan use
#   head=<sha>
#   project_base=<sha>             the diff base
#   base_source=first_checkpoint|head
#   commits_in_range=<n>           commits in project_base..HEAD
#   foreign_commits=<n>            those whose subject is not this plan checkpoint prefix
#   purpose_built=true|false
#   reason=<one phrase>
#   suggested_branch=<name>
#
# project_base is the parent of the first `checkpoint(<plan-slug>)` commit
# reachable from HEAD — where this project started work — or HEAD itself when
# the project has not checkpointed yet. That base survives a resumed run, and it
# excludes commits the branch already carried.
#
# purpose_built is false only when HEAD is detached or sits on the default
# branch. Everything else is a branch the run can treat as its own.

set -euo pipefail

fail() {
  printf 'status=%s\n' "$1"
  printf 'reason=%s\n' "$2"
  exit 0
}

VERB="${1:-}"
WORKING_DIR="${2:-}"

[[ -n "${VERB}" && -n "${WORKING_DIR}" ]] || fail error "usage: style_branch.sh resolve|create <working-dir> [arg]"
cd "${WORKING_DIR}" 2>/dev/null || fail error "working directory not found: ${WORKING_DIR}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "${REPO_ROOT}" ]] || fail not_a_repo "not a git repository: ${WORKING_DIR}"

CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"

resolve_default() {
  local short
  short="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -n "${short}" ]]; then
    DEFAULT_REF="${short}"
    DEFAULT_BRANCH="${short#origin/}"
    return
  fi
  local name
  for name in main master; do
    if git show-ref --verify --quiet "refs/heads/${name}"; then
      DEFAULT_REF="${name}"
      DEFAULT_BRANCH="${name}"
      return
    fi
    if git show-ref --verify --quiet "refs/remotes/origin/${name}"; then
      DEFAULT_REF="origin/${name}"
      DEFAULT_BRANCH="${name}"
      return
    fi
  done
  DEFAULT_REF=""
  DEFAULT_BRANCH=""
}

case "${VERB}" in
resolve)
  PLAN_SLUG="${3:-}"
  resolve_default

  HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  [[ -n "${HEAD_SHA}" ]] || fail error "repository has no commits yet"

  # Scan for this project first checkpoint. Bound the scan to the default
  # branch when there is one, otherwise to recent history.
  if [[ -n "${DEFAULT_REF}" ]] && git rev-parse --verify --quiet "${DEFAULT_REF}" >/dev/null; then
    SCAN_RANGE=("${DEFAULT_REF}..HEAD")
  else
    SCAN_RANGE=("--max-count=500" "HEAD")
  fi

  FIRST_CHECKPOINT=""
  if [[ -n "${PLAN_SLUG}" ]]; then
    while IFS=' ' read -r sha subject; do
      [[ -n "${sha}" ]] || continue
      case "${subject}" in
      "checkpoint(${PLAN_SLUG}):"*) FIRST_CHECKPOINT="${sha}"; break ;;
      esac
    done < <(git log --reverse --format='%H %s' "${SCAN_RANGE[@]}" 2>/dev/null || true)
  fi

  if [[ -n "${FIRST_CHECKPOINT}" ]]; then
    PROJECT_BASE="$(git rev-parse --verify --quiet "${FIRST_CHECKPOINT}^" || true)"
    BASE_SOURCE=first_checkpoint
  fi
  if [[ -z "${PROJECT_BASE:-}" ]]; then
    PROJECT_BASE="${HEAD_SHA}"
    BASE_SOURCE=head
  fi

  COMMITS_IN_RANGE="$(git rev-list --count "${PROJECT_BASE}..HEAD" 2>/dev/null || echo 0)"
  FOREIGN=0
  if [[ -n "${PLAN_SLUG}" && "${COMMITS_IN_RANGE}" != "0" ]]; then
    while IFS= read -r subject; do
      case "${subject}" in
      "checkpoint(${PLAN_SLUG}):"*) ;;
      *) FOREIGN=$((FOREIGN + 1)) ;;
      esac
    done < <(git log --format='%s' "${PROJECT_BASE}..HEAD" 2>/dev/null || true)
  fi

  if [[ -z "${CURRENT_BRANCH}" ]]; then
    PURPOSE_BUILT=false
    REASON="HEAD is detached"
  elif [[ -n "${DEFAULT_BRANCH}" && "${CURRENT_BRANCH}" == "${DEFAULT_BRANCH}" ]]; then
    PURPOSE_BUILT=false
    REASON="on the default branch ${DEFAULT_BRANCH}"
  else
    PURPOSE_BUILT=true
    REASON="on branch ${CURRENT_BRANCH}"
  fi

  SUGGESTED="${PLAN_SLUG:-delegate-run}"

  printf 'status=ok\n'
  printf 'repo_root=%s\n' "${REPO_ROOT}"
  printf 'current_branch=%s\n' "${CURRENT_BRANCH}"
  printf 'default_branch=%s\n' "${DEFAULT_BRANCH}"
  printf 'default_ref=%s\n' "${DEFAULT_REF}"
  printf 'head=%s\n' "${HEAD_SHA}"
  printf 'project_base=%s\n' "${PROJECT_BASE}"
  printf 'base_source=%s\n' "${BASE_SOURCE}"
  printf 'commits_in_range=%s\n' "${COMMITS_IN_RANGE}"
  printf 'foreign_commits=%s\n' "${FOREIGN}"
  printf 'purpose_built=%s\n' "${PURPOSE_BUILT}"
  printf 'reason=%s\n' "${REASON}"
  printf 'suggested_branch=%s\n' "${SUGGESTED}"
  ;;
create)
  BRANCH_NAME="${3:-}"
  [[ -n "${BRANCH_NAME}" ]] || fail error "create needs a branch name"
  git check-ref-format --branch "${BRANCH_NAME}" >/dev/null 2>&1 || fail error "invalid branch name: ${BRANCH_NAME}"
  if git show-ref --verify --quiet "refs/heads/${BRANCH_NAME}"; then
    fail error "branch already exists: ${BRANCH_NAME}"
  fi
  git switch -c "${BRANCH_NAME}" >/dev/null 2>&1 || fail error "could not create branch ${BRANCH_NAME}"
  printf 'status=ok\n'
  printf 'current_branch=%s\n' "$(git branch --show-current)"
  printf 'head=%s\n' "$(git rev-parse HEAD)"
  printf 'project_base=%s\n' "$(git rev-parse HEAD)"
  printf 'base_source=%s\n' "head"
  ;;
*)
  fail error "unknown verb: ${VERB}"
  ;;
esac
