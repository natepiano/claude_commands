#!/bin/bash
# Same JSON fixture as fake_tmux.py; the hot command path needs no Python startup.
set -euo pipefail
state=${FAKE_TMUX_STATE:?}
if [[ ! -f "$state" ]]; then
    printf 'no server running on /tmp/fake\n' >&2
    exit 1
fi
verb=${1:-}
shift || true
unexpected() {
    printf 'fake tmux: unexpected call: %s\n' "$verb $*" >&2
    exit 2
}
# Reject the same malformed calls as the Python fixture. A permissive fake
# would hide a caller that stopped supplying tmux's required flags.
case "$verb" in
    list-panes) [[ $# == 3 && $1 == -a && $2 == -F ]] || unexpected "$@" ;;
    show-environment) [[ $# == 2 && $1 == -t ]] || unexpected "$@" ;;
    set-environment) [[ $# == 4 && $1 == -t ]] || unexpected "$@" ;;
    capture-pane) [[ $# == 4 && $1 == -p && $2 == -e && $3 == -t ]] || unexpected "$@" ;;
    send-keys) [[ $# -ge 2 && $1 == -t ]] || unexpected "$@" ;;
    *) unexpected "$@" ;;
esac
case "$verb" in
    list-panes)
        exec jq -r 'to_entries[] | .key as $key | .value | .label as $label | .panes[] | [$key, ., $label] | join("\t")' "$state"
        ;;
    show-environment|set-environment|capture-pane|send-keys)
        if [[ "$verb" == capture-pane ]]; then shift 2; fi
        [[ ${1:-} == -t ]] || exit 2
        target=$2
        shift 2
        # Select the first matching session, as the Python fixture does.
        selection='to_entries | map(select(.key == $target or .value.label == $target or ("=" + .value.label) == $target or (.value.panes | index($target)))) | .[0]'
        case "$verb" in
            show-environment)
                exec jq -r --arg target "$target" "$selection | if . == null then (\"can't find session: \" + \$target + \"\\n\" | halt_error(1)) else .value.env | to_entries[]? | \"\(.key)=\(.value)\" end" "$state"
                ;;
            capture-pane)
                exec jq -r --arg target "$target" "$selection | if . == null then (\"can't find pane: \" + \$target + \"\\n\" | halt_error(1)) else .value.screens[\$target] // \"\" end" "$state"
                ;;
            set-environment)
                update='($chosen.key) as $key | .[$key].env[$name] = $value'
                fields=(--arg name "$1" --arg value "$2")
                ;;
            send-keys)
                update='($chosen.key) as $key | .[$key].sent = ((.[$key].sent // []) + [[$target] + $ARGS.positional])'
                ;;
        esac
        temporary="$state.$$.tmp"
        trap 'rm -f "$temporary"' EXIT
        # jq arguments precede its filter; positional keys follow the input file.
        if [[ "$verb" == send-keys ]]; then
            jq -n --slurpfile state "$state" --arg target "$target" --args "\$state[0] | ($selection) as \$chosen | if \$chosen == null then (\"can't find pane: \" + \$target + \"\\n\" | halt_error(1)) else $update end" -- "$@" > "$temporary"
        else
            jq --arg target "$target" "${fields[@]}" "($selection) as \$chosen | if \$chosen == null then (\"can't find session: \" + \$target + \"\\n\" | halt_error(1)) else $update end" "$state" > "$temporary"
        fi
        mv "$temporary" "$state"
        ;;
    *) printf 'fake tmux: unexpected call: %s\n' "$verb $*" >&2; exit 2 ;;
esac
