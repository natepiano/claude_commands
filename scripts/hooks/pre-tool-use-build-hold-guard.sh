#!/usr/bin/env bash
# Start Python only for possible build calls with a hold directory entry.

IFS= read -r -d '' hook_input || true
case "$hook_input" in
    *cargo*|*merge_gate*) ;;
    *) exit 0 ;;
esac

hold_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}"
shopt -s nullglob dotglob
if [[ ! -e "$hold_dir" || ( -d "$hold_dir" && -r "$hold_dir" && -x "$hold_dir" ) ]]; then
    hold_entries=("$hold_dir"/*)
    (( ${#hold_entries[@]} )) || exit 0
fi

hook_dir="${BASH_SOURCE[0]%/*}"
printf '%s' "$hook_input" | "$hook_dir/../lib/py" "$hook_dir/pre-tool-use-build-hold-guard.py"
