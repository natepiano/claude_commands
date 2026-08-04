#!/bin/bash
# Check lint-runs cache and return actionable results.
# Called by the /clippy command as a single step.
#
# Exit codes:
#   0 = cache hit (fresh results available)
#   1 = cache miss (agent should run the lint suite itself)
#
# Output format (on exit 0):
#   cached: <timestamp>
#   lint mend         : passed | N auto-fixable, M manual
#   lint fmt          : unknown (not cached)
#   lint clippy       : passed | issues found
#   lint doc          : passed | issues found
#   git diff          : clean | has changes
#   resume: <directive telling /clippy which STEP to re-enter at>
#   === lint mend (manual) ===   per-rule aggregate, then the finding blocks
#                                that `mend --fix` cannot apply
#   === lint clippy ===
#   === lint doc ===
#   logs: <state dir>
#
# Per-command pass/fail comes from latest.json's `commands[]` array — its
# `status` and `log_file` fields are authoritative, so there is no guessing from
# log text and no reliance on legacy log filenames.
#
# The one thing latest.json does NOT record is which mend findings `mend --fix`
# can apply on its own. That split decides where /clippy resumes: auto-fixable
# findings must go through STEP 3 (`lint mend --fix`), and only the manual ones
# belong in the batch approval gate. So the mend log is parsed for exactly that,
# and only the manual findings are printed.
#
# The working tree diff is reported as clean/has-changes but never dumped —
# /clippy's style-review step builds its own diff from git.
#
# Usage: check_cache.sh [project_dir]
#   project_dir defaults to current directory
#
# Env:
#   MEND_MAX_LINES   cap on printed manual-finding lines (default 1200)

set -euo pipefail

MEND_MAX_LINES="${MEND_MAX_LINES:-1200}"

cache_root() {
    if [[ -n "${XDG_CACHE_HOME:-}" ]]; then
        printf '%s\n' "$XDG_CACHE_HOME/cargo-port"
    elif [[ -n "${LOCALAPPDATA:-}" ]]; then
        printf '%s\n' "$LOCALAPPDATA/cargo-port"
    elif [[ "$OSTYPE" == darwin* ]]; then
        printf '%s\n' "$HOME/Library/Caches/cargo-port"
    else
        printf '%s\n' "$HOME/.cache/cargo-port"
    fi
}

PROJECT_DIR="$(cd "${1:-.}" && pwd -P)"
TEMP_ROOT="$(cache_root)/lint-runs"
STALE_SECONDS=1800  # 30 minutes

# SYNC: must match cargo-port's project_key() in src/lint/paths.rs.
# Format: {basename}-{first 16 hex chars of SHA-256 of absolute path}
project_key() {
    local name hash
    name="$(basename "$1")"
    hash="$(printf '%s' "$1" | shasum -a 256 | cut -c1-16)"
    printf '%s' "${name}-${hash}"
}

STATE_DIR="$TEMP_ROOT/$(project_key "$PROJECT_DIR")"
LATEST_FILE="$STATE_DIR/latest.json"

if [[ ! -f "$LATEST_FILE" ]]; then
    echo "No latest.json found." >&2
    exit 1
fi

# Read the whole run record in one pass and emit shell-quoted assignments.
# Anything absent comes back as an empty string rather than an unset variable.
run_meta() {
    python3 - "$LATEST_FILE" <<'PY'
import json
import shlex
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit(1)

if not isinstance(data, dict):
    raise SystemExit(1)


def emit(key, value):
    print(f"{key}={shlex.quote('' if value is None else str(value))}")


emit("RUN_STATUS", data.get("status"))
emit("RUN_STARTED", data.get("started_at"))
emit("RUN_FINISHED", data.get("finished_at"))

known = {"mend", "clippy", "doc", "fmt"}
seen = set()
for cmd in data.get("commands") or []:
    if not isinstance(cmd, dict):
        continue
    name = str(cmd.get("name") or "").lower()
    if name not in known:
        continue
    seen.add(name)
    emit(f"CMD_{name.upper()}_STATUS", cmd.get("status"))
    emit(f"CMD_{name.upper()}_LOG", cmd.get("log_file"))

for name in known - seen:
    emit(f"CMD_{name.upper()}_STATUS", "")
    emit(f"CMD_{name.upper()}_LOG", "")
PY
}

if ! meta="$(run_meta)"; then
    echo "latest.json is unreadable or malformed." >&2
    exit 1
fi
eval "$meta"

if [[ -z "$RUN_STATUS" || -z "$RUN_STARTED" ]]; then
    echo "latest.json is missing required fields." >&2
    exit 1
fi

parse_timestamp_epoch() {
    python3 - "$1" <<'PY'
from datetime import datetime
import sys

value = sys.argv[1].strip()
if not value:
    print(0)
    raise SystemExit(0)

try:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
except ValueError:
    print(0)
    raise SystemExit(0)

print(int(dt.timestamp()))
PY
}

if [[ "$RUN_STATUS" == "running" ]]; then
    start_epoch=$(parse_timestamp_epoch "$RUN_STARTED")
    now_epoch=$(date "+%s")
    age=$(( now_epoch - start_epoch ))
    if (( start_epoch == 0 || age > STALE_SECONDS )); then
        echo "lint-runs run is stale (${age}s ago)." >&2
        exit 1
    fi

    echo "lint-runs is running, waiting for results..." >&2
    timeout=300
    elapsed=0
    while (( elapsed < timeout )); do
        sleep 2
        elapsed=$(( elapsed + 2 ))
        meta="$(run_meta)" || break
        eval "$meta"
        if [[ "$RUN_STATUS" != "running" ]]; then
            break
        fi
    done

    if [[ "$RUN_STATUS" == "running" ]]; then
        echo "Timed out waiting for lint-runs (${timeout}s)." >&2
        exit 1
    fi
fi

case "$RUN_STATUS" in
    passed|failed) ;;
    *)
        echo "Unsupported lint-runs status: $RUN_STATUS" >&2
        exit 1
        ;;
esac

fresh_timestamp="$RUN_FINISHED"
if [[ -z "$fresh_timestamp" ]]; then
    fresh_timestamp="$RUN_STARTED"
fi

log_epoch=$(parse_timestamp_epoch "$fresh_timestamp")
if (( log_epoch == 0 )); then
    echo "Could not parse timestamp: $fresh_timestamp" >&2
    exit 1
fi

# rg honours .gitignore, so target/ stays out. getmtime keeps this off BSD-only
# `stat -f`, whose GNU coreutils spelling is different.
newest_source_mtime=$(
    rg --files -g '*.rs' -g '*.toml' "$PROJECT_DIR" 2>/dev/null |
    python3 -c '
import os
import sys

newest = 0.0
found = False
for line in sys.stdin:
    path = line.rstrip("\n")
    if not path:
        continue
    try:
        newest = max(newest, os.path.getmtime(path))
        found = True
    except OSError:
        pass
print(int(newest) if found else "")
'
)

if [[ -z "$newest_source_mtime" ]]; then
    echo "No source files found." >&2
    exit 1
fi

if (( newest_source_mtime > log_epoch )); then
    echo "Cache stale — source files changed after last lint-runs." >&2
    exit 1
fi

# latest.json records log_file relative to the state dir. Fall back to the
# stable <name>-latest.log mirror when the per-run copy has been reaped.
resolve_log() {
    local rel="$1" name="$2"
    if [[ -n "$rel" && -f "$STATE_DIR/$rel" ]]; then
        printf '%s\n' "$STATE_DIR/$rel"
        return 0
    fi
    if [[ -f "$STATE_DIR/${name}-latest.log" ]]; then
        printf '%s\n' "$STATE_DIR/${name}-latest.log"
        return 0
    fi
    return 1
}

# Split mend findings into what `mend --fix` applies and what a human must do.
# Writes the manual detail (per-rule aggregate first, then the finding blocks)
# to $2; prints the fixable count and the manual count, one per line.
analyze_mend() {
    python3 - "$1" "$2" "$MEND_MAX_LINES" <<'PY'
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

log_path, out_path, max_lines = sys.argv[1], sys.argv[2], int(sys.argv[3])

try:
    text = Path(log_path).read_text(errors="replace")
except OSError:
    print(0)
    print(0)
    raise SystemExit(0)

AUTOFIX = "auto-fixable with `cargo mend --fix`"
RULE = re.compile(r"cargo-mend#([a-z-]+)")
LOCATION = re.compile(r"-->\s*([^\s:]+):")

fixable = 0
manual = []
trailer = []

for block in text.split("\n\n"):
    if not block.strip():
        continue
    if RULE.search(block):
        if AUTOFIX in block:
            fixable += 1
        else:
            manual.append(block.rstrip("\n"))
    elif re.match(r"\s*(errors|summary):", block):
        trailer.extend(line.strip() for line in block.splitlines() if line.strip())

rules = Counter()
files = defaultdict(set)
for block in manual:
    rule = RULE.search(block)
    if not rule:
        continue
    rules[rule.group(1)] += 1
    location = LOCATION.search(block)
    if location:
        files[rule.group(1)].add(location.group(1))

header = []
if rules:
    header.append("manual findings by rule:")
    for rule, count in rules.most_common():
        header.append(f"  {count:5}  {rule}  ({len(files[rule])} files)")
if trailer:
    header.extend(trailer)
if header:
    header.append("")

body = []
for block in manual:
    body.extend(block.splitlines())
    body.append("")

if len(body) > max_lines:
    body = body[:max_lines]
    body.append(f"... truncated at {max_lines} lines — read {log_path} for the rest")

Path(out_path).write_text("\n".join(header + body).rstrip("\n") + "\n")

print(fixable)
print(len(manual))
PY
}

mend_log="$(resolve_log "$CMD_MEND_LOG" mend || true)"
clippy_log="$(resolve_log "$CMD_CLIPPY_LOG" clippy || true)"
doc_log="$(resolve_log "$CMD_DOC_LOG" doc || true)"

# Always analyse the mend log when one exists. cargo-port only fails the mend
# command when it was invoked with --fail-on-warn, so a `passed` status can still
# sit on top of auto-fixable warnings; the counts decide, not the status.
mend_fixable=0
mend_manual=0
# Written beside the run's own logs rather than to TMPDIR: the state dir is
# always writable when the cache exists, and the file survives the run so a
# truncated listing can be read back in full.
mend_detail="$STATE_DIR/mend-manual-latest.log"
if [[ -n "$mend_log" ]]; then
    counts="$(analyze_mend "$mend_log" "$mend_detail")"
    mend_fixable="$(printf '%s\n' "$counts" | head -1)"
    mend_manual="$(printf '%s\n' "$counts" | tail -1)"
elif [[ "$CMD_MEND_STATUS" == "failed" ]]; then
    echo "mend failed but its log is missing." >&2
    exit 1
fi

clippy_has_issues=false
if [[ "$CMD_CLIPPY_STATUS" == "failed" ]]; then
    clippy_has_issues=true
fi

doc_has_issues=false
if [[ "$CMD_DOC_STATUS" == "failed" ]]; then
    doc_has_issues=true
fi

diff_output="$(git -C "$PROJECT_DIR" diff 2>/dev/null || true)"
untracked="$(git -C "$PROJECT_DIR" ls-files --others --exclude-standard 2>/dev/null || true)"
tree_state=clean
if [[ -n "$diff_output" || -n "$untracked" ]]; then
    tree_state="has changes"
fi

display_timestamp="${fresh_timestamp/T/ }"
display_timestamp="${display_timestamp%%[-+][0-9][0-9]:[0-9][0-9]}"

echo "cached: $display_timestamp"
if (( mend_fixable > 0 || mend_manual > 0 )); then
    echo "lint mend         : ${mend_fixable} auto-fixable, ${mend_manual} manual"
else
    echo "lint mend         : passed"
fi
echo "lint fmt          : unknown (not cached)"
if [[ "$clippy_has_issues" == true ]]; then
    echo "lint clippy       : issues found"
else
    echo "lint clippy       : passed"
fi
if [[ "$doc_has_issues" == true ]]; then
    echo "lint doc          : issues found"
else
    echo "lint doc          : passed"
fi
echo "git diff          : $tree_state"

# Where /clippy re-enters its pipeline. Auto-fixable mend findings must be
# applied by STEP 3 before anything is offered to the batch gate; applying them
# rewrites source, which invalidates the cached clippy and doc results, so that
# path re-runs them. With nothing to auto-fix the cached results still describe
# the tree, so STEP 5/5b can read them instead of re-running.
if (( mend_fixable > 0 )); then
    echo "resume: STEP 3 — ${mend_fixable} auto-fixable mend findings; run 3 -> 4 -> 5 -> 5b -> 6 -> 7 -> 8 -> 9"
elif (( mend_manual > 0 )) || [[ "$clippy_has_issues" == true || "$doc_has_issues" == true ]]; then
    echo "resume: STEP 4 — nothing to auto-fix; run 4 -> 6 -> 7 -> 8 -> 9, reusing the cached clippy/doc output below for STEP 5/5b unless STEP 4 edits files"
elif [[ "$tree_state" == "has changes" ]]; then
    echo "resume: STEP 4 — all lints passed; run 4 -> 6 -> 8 -> 9, reusing the cached clippy/doc results for STEP 5/5b unless STEP 4 edits files"
else
    echo "resume: NONE — all lints passed and the working tree is clean"
fi

if (( mend_manual > 0 )); then
    echo "=== lint mend (manual) ==="
    cat "$mend_detail"
    echo "(full manual listing: $mend_detail)"
fi
if (( mend_fixable > 0 )); then
    echo "=== lint mend (auto-fixable) ==="
    echo "${mend_fixable} findings apply cleanly via \`lint mend --fix\` — not listed; run STEP 3."
fi
if [[ "$clippy_has_issues" == true && -n "$clippy_log" ]]; then
    echo "=== lint clippy ==="
    cat "$clippy_log"
fi
if [[ "$doc_has_issues" == true && -n "$doc_log" ]]; then
    echo "=== lint doc ==="
    cat "$doc_log"
fi

echo "logs: $STATE_DIR"
