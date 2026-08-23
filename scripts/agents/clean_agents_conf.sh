#!/usr/bin/env bash

set -euo pipefail

# Git clean filter for config/agents.conf.
#
# The file churns constantly: /agent rewrites model:effort rows and
# sync_codex_catalog.sh regenerates [codex.agents]. None of that is worth a
# commit, so this pins the staged content to whatever is already in the index —
# git then sees no change and the retuning never surfaces.
#
# Unlike the settings.json filter, this cannot simply delete the volatile rows.
# agents_config.sh validates every agent and effort and hard-errors on an empty
# one, so a stripped file would fail on a fresh clone and take delegate,
# clean-fix, and the CLI aliases down with it. Pinning keeps the committed copy
# complete and valid.
#
# To commit a real structural change (a new function, subtask, or section):
#
#   touch config/agents.conf && AGENTS_CONF_COMMIT=1 git add config/agents.conf
#
# The touch is load-bearing. Once git has refreshed its stat cache for a path it
# considers up to date, it compares nothing and runs no filter, so the override
# reads as inert until the mtime moves again. Any edit does that on its own;
# only a re-run against an already-refreshed file needs the touch.
#
# Falls back to passing the content through whenever the pin cannot be read —
# an untracked first add, or a repo state with no index entry — so the filter
# can never block a commit outright.

if [[ "${AGENTS_CONF_COMMIT:-}" == "1" ]]; then
  exec cat
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if git -C "$REPO_ROOT" rev-parse --verify --quiet :config/agents.conf >/dev/null 2>&1; then
  # Drain stdin before emitting the pin: git keeps writing the working-tree
  # content into this side of the pipe, and leaving it unread races a SIGPIPE.
  cat >/dev/null
  # cat-file rather than a command substitution: $(...) strips trailing
  # newlines, so the pin has to stream straight through to stay byte-exact.
  exec git -C "$REPO_ROOT" cat-file blob :config/agents.conf
fi

exec cat
