#!/usr/bin/env bash
# Excludes a file from git tracking via .git/info/exclude
# Usage: exclude.sh <filename>
# Returns: Status of each operation performed
# Exits 1, adding no line and untracking nothing, when git would still not ignore the file

FILENAME="$1"

if [[ -z "$FILENAME" ]]; then
    echo "Error: No filename provided"
    exit 1
fi

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

EXCLUDE_FILE=$(git rev-parse --git-path info/exclude)

# Ensure .git/info/exclude exists
mkdir -p "$(dirname "$EXCLUDE_FILE")"
touch "$EXCLUDE_FILE"

# A leading slash anchors a pattern to the repo root, so git is asked about the path without it.
# An absolute path that exists is no such pattern, and is asked about as given.
CHECK_PATH="$FILENAME"
if [[ "$FILENAME" == /[!/]* && ! -e "$FILENAME" ]]; then
    CHECK_PATH="${FILENAME#/}"
fi

# Exit 1 unless git's rules now ignore the file: a .gitignore rule outranks .git/info/exclude.
# With "appended", first take back the line this run added, which has no effect.
require_ignored() {
    git check-ignore -q --no-index -- "$CHECK_PATH"
    local code=$?
    [[ $code -eq 0 ]] && return
    if [[ "$1" == "appended" ]]; then
        grep -vxF -- "$FILENAME" "$EXCLUDE_FILE" > "$EXCLUDE_FILE.tmp"
        mv "$EXCLUDE_FILE.tmp" "$EXCLUDE_FILE"
    fi
    local reason="git check-ignore failed"
    if [[ $code -eq 1 ]]; then
        reason=$(git check-ignore -v --no-index -- "$CHECK_PATH" | cut -f1)
        reason="${reason:+$reason re-includes it}"
        reason="${reason:-no ignore rule matches it}"
    fi
    echo "Error: $FILENAME is NOT excluded: $reason"
    exit 1
}

# Check if already excluded
if grep -qxF "$FILENAME" "$EXCLUDE_FILE"; then
    require_ignored
    echo "ALREADY_EXCLUDED: $FILENAME is already listed in .git/info/exclude"
    exit 0
fi

# Append to exclude, before the file leaves the index, so a refusal changes nothing
echo "$FILENAME" >> "$EXCLUDE_FILE"
require_ignored appended

# Check if tracked and remove from tracking if so
WAS_TRACKED=false
if git ls-files --error-unmatch "$FILENAME" >/dev/null 2>&1; then
    WAS_TRACKED=true
    git rm --cached "$FILENAME" >/dev/null 2>&1
    echo "UNTRACKED: Removed $FILENAME from git tracking (local file kept)"
fi

echo "EXCLUDED: Added $FILENAME to .git/info/exclude"

if [[ "$WAS_TRACKED" == "false" ]]; then
    echo "NOTE: File was not tracked by git, only added to exclude list"
fi
