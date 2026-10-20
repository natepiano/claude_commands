#!/usr/bin/env bash
# worker_name.sh — Print the mesh name a delegate worker launches under.
#
# Usage: worker_name.sh <working_dir> <slot> <kind> [prefix]
#   prefix — replaces the project name; implement.sh passes its 10th argument
#
# The name is `<project>-<slot>`: `hana_catalyst-impl`, `hana_catalyst-test`.
# A repair worker -- slot impl, kind fix -- is `<project>-fix`.
#
# One script owns the rule because two callers need it: implement.sh launches
# the worker under this name, and the unit director writes it into the peer's
# prompt before that launch. Two copies of the rule would drift, and a peer
# told the wrong name sends into nothing.
#
# <project> is the directory name of the working tree's git top level, so a
# worktree is named for itself, the same name its interactive session carries
# in `claude agents`. The session registry is machine-wide, and the project
# name is what keeps two runs in two worktrees apart. A name shared with a
# finished worker still alive from an earlier phase is not silent: ListAgents
# lists both and SendMessage asks for the row's ref.
#
# Every other slot is named for the slot, never its opening role: a `test` worker
# opening as a writer stays `-test`, or both writers would be `-impl`. The
# repair worker is the exception because it runs alone, and because the phase's
# `-impl` worker is left alive after its turn -- a repair also named `-impl`
# would share its address.

set -euo pipefail

WORKING_DIR="${1:?Usage: worker_name.sh <working_dir> <slot> <kind> [prefix]}"
SLOT="${2:?missing slot}"
KIND="${3:?missing kind}"
PREFIX="${4:-}"

if [[ -z "${PREFIX}" ]]; then
  TOP="$(git -C "${WORKING_DIR}" rev-parse --show-toplevel 2>/dev/null || printf '%s' "${WORKING_DIR}")"
  PREFIX="$(basename "${TOP}")"
fi
PREFIX="$(printf '%s' "${PREFIX}" | tr -c '[:alnum:]._-' '-' | cut -c1-40)"

if [[ "${SLOT}" == "impl" && "${KIND}" == "fix" ]]; then
  SUFFIX="fix"
else
  SUFFIX="${SLOT}"
fi

printf '%s-%s\n' "${PREFIX}" "${SUFFIX}"
