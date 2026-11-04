"""Shell stand-ins keep argument records without starting Python per command."""

from pathlib import Path


JSON_SHELL = r'''
json_string() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    value=${value//$'\b'/\\b}
    value=${value//$'\f'/\\f}
    printf '"%s"' "$value"
}
json_args() {
    local separator='' argument
    printf '['
    for argument in "$@"; do
        printf '%s' "$separator"
        json_string "$argument"
        separator=,
    done
    printf ']'
}
record_command() {
    printf '{"command":'
    json_string "${0##*/}"
    printf ',"args":'
    json_args "$@"
}
'''


def write_shell(path: Path, source: str) -> None:
    """Bash builtins handle JSON escaping and timed FIFO reads on Linux and macOS."""
    _ = path.write_text("#!/bin/bash\n" + JSON_SHELL + source)
    path.chmod(0o755)


COMMAND_STUB = r'''
root=$MAC_TEST_STUB_DIR
source "$root/fixture.sh"
command=${0##*/}
if [[ $command == systemd-run ]]; then
    json_args "$@" >> "$root/systemd-run.jsonl"
    printf '\n' >> "$root/systemd-run.jsonl"
    exit 0
fi
{
    record_command "$@"
    claim_file=$MAC_TEST_STATE_DIR/claims/hana.json
    if [[ -f $claim_file ]]; then
        IFS= read -r -d '' claim < "$claim_file" || :
        printf ',"claimed":true,"claim":%s' "${claim//$'\n'/}"
    else
        printf ',"claimed":false'
    fi
    if [[ $command == rsync ]]; then
        for argument in "$@"; do
            if [[ $argument == --exclude-from=* ]]; then
                IFS= read -r -d '' exclude < "${argument#*=}" || :
                printf ',"exclude":'
                json_string "$exclude"
                break
            fi
        done
    fi
    printf '}\n'
} >> "$root/events.jsonl"

await_release() {
    # Open both ends so timeout covers the read, including a missing writer.
    IFS= read -r -t "$TEST_HANG_GUARD_SECS" reply <> "$root/$1" || exit 98
}
if [[ $command == rsync ]]; then
    printf 'started\n' > "$root/copy-started"
    if (( hold_copy )); then await_release continue-copy; fi
    exit "$rsync_status"
fi
if [[ $command != ssh ]]; then
    printf 'called\n' > "$root/$command-called"
    exit 99
fi
remote=${!#}
if [[ $remote == *'select_leftovers()'* && $remote == *lsof* ]]; then
    printf 'called\n' > "$root/cleanup-called"
    if (( hold_cleanup )); then
        printf 'started\n' > "$root/cleanup-started"
        await_release continue-cleanup
    fi
    if [[ -n $selector_shell ]]; then
        export PATH="$root/selectors:$PATH"
        checks=''
        for name in pgrep lsof sleep; do
            [[ $(command -v "$name") == "$root/selectors/$name" ]] || exit 99
            printf -v check '[ "$(command -v %s)" = %q ] || exit 99\n' "$name" "$root/selectors/$name"
            checks+=$check
        done
        if [[ $selector_shell == sh ]]; then shell=(/bin/sh); else shell=(zsh -f); fi
        "${shell[@]}" -c "$checks$remote" > "$root/selector-answer"
        status=$?
        while IFS= read -r line; do printf '%s\n' "$line"; done < "$root/selector-answer"
        exit "$status"
    fi
    if [[ -n $cleanup_error ]]; then printf '%s\n' "$cleanup_error" >&2; fi
    for line in "${cleanup_lines[@]}"; do printf '%s\n' "$line"; done
    exit "$cleanup_status"
fi
if [[ $remote == *'lint nextest'* || $remote == *'lint check'* || $remote == *'lint clippy'* ||
      $remote == *'lint mend-check'* || $remote == *'lint doc'* ]]; then
    printf 'started\n' > "$root/run-started"
    if (( hold_run )); then await_release finish-run; fi
    for index in "${!run_lines[@]}"; do
        if [[ ${run_streams[index]} == stdout ]]; then
            printf '%s\n' "${run_lines[index]}"
        else
            printf '%s\n' "${run_lines[index]}" >&2
        fi
        if (( index == 0 && hold_after_first_line )); then
            printf 'sent\n' > "$root/first-line-sent"
            await_release continue-run
        fi
    done
    if (( run_status_reported )); then
        printf '%s\n' "$run_status_line"
        if [[ -n $run_cleanup_line ]]; then printf '%s\n' "$run_cleanup_line"; fi
    fi
    exit "$run_ssh_status"
fi
printf 'started\n' > "$root/probe-started"
if (( hold_probe )); then await_release continue-probe; fi
if [[ -n $probe_error ]]; then printf '%s\n' "$probe_error" >&2; fi
for line in "${probe_lines[@]}"; do printf '%s\n' "$line"; done
exit "$probe_status"
'''


SELECTOR_STUB = r'''
root=$MAC_TEST_STUB_DIR
source "$root/fixture.sh"
{ record_command "$@"; printf '}\n'; } >> "$root/selector-events.jsonl"
case ${0##*/} in
    pgrep) exit "$selector_pgrep_status" ;;
    lsof) exit "$selector_lsof_status" ;;
    sleep) exit 0 ;;
    *) exit 99 ;;
esac
'''


ACTIVITY_SELECTOR_STUB = r'''
root=$ACTIVITY_TEST_DIR
{ record_command "$@"; printf '}\n'; } >> "$root/commands.jsonl"
source "$root/${0##*/}-response.sh"
printf '%s' "$answer"
exit "$status"
'''
