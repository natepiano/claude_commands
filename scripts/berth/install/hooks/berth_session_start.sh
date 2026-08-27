#!/usr/bin/env bash

# Reconcile once at SessionStart and surface only actionable current conditions.
# Persistent bypass audit history remains on the board and is not re-announced.

set -u

emit_session_feedback() {
    local summary=$1 detail=$2

    jq -cn --arg summary "$summary" --arg detail "$detail" '{
        systemMessage: $summary,
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $detail
        }
    }'
}

valid_common_envelope() {
    local expected_exit_code=$1

    jq -e --argjson expected_exit_code "$expected_exit_code" '
        (type == "object") and
        (.verb == "board") and
        (.status | type == "string") and
        (.exit_code == $expected_exit_code) and
        (.reservations | type == "array") and
        (.blocked_by | type == "array") and
        (.message | type == "string") and
        (.payload | type == "object") and
        (.payload.kind | type == "string") and
        (.payload.alerts | type == "array")
    ' "$command_stdout_file" >/dev/null 2>&1
}

valid_no_facts_response() {
    local expected_exit_code=$1 expected_status=$2

    valid_common_envelope "$expected_exit_code" && jq -e --arg expected_status "$expected_status" '
        (.status == $expected_status) and
        (.reservations | length == 0) and
        (.blocked_by | length == 0) and
        (.payload.kind == "no_facts")
    ' "$command_stdout_file" >/dev/null 2>&1
}

valid_board_envelope() {
    valid_common_envelope 0 && jq -e '
        def nonempty_string: type == "string" and length > 0;
        def valid_orphan:
            (.reservation_id | nonempty_string) and
            (.protected_tip | nonempty_string) and
            (.resolution | type == "object") and
            ((.resolution.action == "recover" and (.resolution.flag | nonempty_string)) or
             (.resolution.action == "retire_or_abandon" and
                (.resolution.flags | type == "array" and length > 0) and
                all(.resolution.flags[]; nonempty_string)));
        def valid_unrecorded_bypasses:
            (.count | type == "number") and (.count > 0) and
            (.occurrence_times | type == "array") and
            (.instruction | nonempty_string);
        def valid_alert:
            (type == "object") and
            (if .kind == "orphaned_outstanding" then valid_orphan
             elif .kind == "unrecorded_bypasses" then valid_unrecorded_bypasses
             else true
             end);
        def valid_incursion:
            (type == "object") and
            (.incident_id | nonempty_string) and
            (.straying_reservation_id | nonempty_string) and
            (.foreign_reservation_ids | type == "array" and length > 0) and
            all(.foreign_reservation_ids[]; nonempty_string) and
            (.entered_paths | type == "array" and length > 0) and
            all(.entered_paths[]; nonempty_string) and
            (.resolution | type == "object") and
            (.resolution.reservation_id | nonempty_string) and
            (.resolution.incident_id | nonempty_string) and
            (.resolution.flag | nonempty_string) and
            (.resolution.every_flag | nonempty_string) and
            (.outstanding_count | type == "number" and . >= 1);
        (.status == "board_ready") and
        (.blocked_by | length == 0) and
        (.payload.kind == "board") and
        (.payload.data | type == "object") and
        (.payload.data.recovered_bypasses_this_invocation | type == "array") and
        all(.payload.data.recovered_bypasses_this_invocation[]; nonempty_string) and
        (.payload.data.alerts.entries | type == "array") and
        all(.payload.data.alerts.entries[]; valid_alert) and
        (.payload.data.outstanding_incursions.entries | type == "array") and
        all(.payload.data.outstanding_incursions.entries[]; valid_incursion)
    ' "$command_stdout_file" >/dev/null 2>&1
}

session_notices() {
    jq -r '
        def resolution_command($reservation_id):
            "cargo-berth " + sub("^resolve "; "resolve " + $reservation_id + " ");
        [
            (.payload.data.recovered_bypasses_this_invocation[]? |
                "RECOVERED BYPASS: pending marker \(.) was imported into the journal and drained during this SessionStart. This is a one-time recovery notice; retained bypass audit history is not a standing alert."),
            (.payload.data.alerts.entries[]? |
                select(.kind == "orphaned_outstanding") |
                . as $alert |
                (if $alert.resolution.action == "recover" then
                    [($alert.resolution.flag | resolution_command($alert.reservation_id))]
                 else
                    [$alert.resolution.flags[] | resolution_command($alert.reservation_id)]
                 end) as $commands |
                "ORPHANED OUTSTANDING: reservation \($alert.reservation_id) at protected tip \($alert.protected_tip) is \($alert.recoverability). Answer it with \($commands | map("`" + . + "`") | join(" or ")) after reviewing the work."),
            (.payload.data.alerts.entries[]? |
                select(.kind == "unrecorded_bypasses") |
                "UNRECORDED BYPASS: \(.count) pending bypass marker(s) still could not be journalled. \(.instruction). This condition will be reported at every SessionStart until it becomes durable."),
            (.payload.data.outstanding_incursions.entries[]? |
                "OUTSTANDING INCURSION: reservation \(.straying_reservation_id) entered \(.entered_paths | join(", ")), held by \(.foreign_reservation_ids | join(", ")); incident \(.incident_id). " +
                (if .outstanding_count > 1 then
                    "This reservation has \(.outstanding_count) outstanding incidents, so answering this one leaves \(.outstanding_count - 1). STOP and answer them all with `cargo-berth \(.resolution.every_flag)`, or this one alone with `cargo-berth \(.resolution.flag)`."
                 else
                    "STOP and answer it with `cargo-berth \(.resolution.flag)`."
                 end) +
                " It will stop appearing after a disposition is recorded.")
        ] | join("\n")
    ' "$command_stdout_file"
}

cleanup_command_capture() {
    rm -f "$command_stdout_file" "$command_stderr_file"
    rmdir "$command_capture_directory" 2>/dev/null || true
}

if ! hook_payload=$(jq -c . 2>/dev/null); then
    emit_session_feedback 'cargo-berth rejected an invalid SessionStart payload.' 'SessionStart stdin was not valid JSON, so reconciliation did not run. Run `cargo-berth board --json` by hand.'
    exit 0
fi

if payload_working_directory=$(printf '%s' "$hook_payload" | jq -er '
    .cwd | select(type == "string" and length > 0)
' 2>/dev/null); then
    :
else
    payload_working_directory=$PWD
fi

if [ ! -d "$payload_working_directory" ]; then
    emit_session_feedback 'cargo-berth could not reconcile this session.' "Hook working directory $payload_working_directory does not exist. Run \`cargo-berth board --json\` by hand."
    exit 0
fi

command_capture_directory=$(mktemp -d "${TMPDIR:-/tmp}/cargo-berth-session-start.XXXXXX") || {
    emit_session_feedback 'cargo-berth could not reconcile this session.' 'The hook could not create command capture files. Run `cargo-berth board --json` by hand.'
    exit 0
}
command_stdout_file=$command_capture_directory/stdout
command_stderr_file=$command_capture_directory/stderr
trap cleanup_command_capture EXIT HUP INT TERM

# --json is mandatory: bare board may open the full-screen terminal view.
if (cd "$payload_working_directory" && cargo-berth board --json) \
    >"$command_stdout_file" 2>"$command_stderr_file"; then
    command_exit_code=0
else
    command_exit_code=$?
fi

case "$command_exit_code" in
    0)
        if ! valid_board_envelope; then
            emit_session_feedback 'cargo-berth rejected an untrusted board response.' 'SessionStart board output was malformed or used an unknown status. Run `cargo-berth board --json` by hand.'
            exit 0
        fi
        actionable_notices=$(session_notices)
        [ -n "$actionable_notices" ] || exit 0
        notice_count=$(printf '%s\n' "$actionable_notices" | awk 'END { print NR }')
        emit_session_feedback "cargo-berth found $notice_count actionable coordination notice(s)." "$actionable_notices"
        ;;
    4)
        if valid_no_facts_response 4 unconfigured; then
            exit 0
        fi
        if valid_no_facts_response 4 ledger_unreadable; then
            ledger_detail=$(jq -r '.message' "$command_stdout_file")
            emit_session_feedback 'cargo-berth could not read the reservation ledger at SessionStart.' "$ledger_detail Run \`cargo-berth board --json\` again after repairing the ledger."
        else
            emit_session_feedback 'cargo-berth rejected an untrusted board response.' 'SessionStart board exited 4 without a valid unconfigured or ledger-unreadable envelope.'
        fi
        ;;
    6)
        if valid_no_facts_response 6 contention; then
            contention_detail=$(jq -r '.message' "$command_stdout_file")
            emit_session_feedback 'cargo-berth exhausted its ledger-lock deadline at SessionStart.' "$contention_detail The engine already spent its single 10-second retry budget; the hook did not invoke board again. Run \`cargo-berth board --json\` when the ledger is free."
        else
            emit_session_feedback 'cargo-berth rejected an untrusted board response.' 'SessionStart board exited 6 without a valid contention envelope.'
        fi
        ;;
    *)
        emit_session_feedback 'cargo-berth returned an unreachable board exit.' "SessionStart board exited $command_exit_code. Headless board cannot return terminal-view exit 7."
        ;;
esac

exit 0
