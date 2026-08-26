#!/usr/bin/env bash

# Observe working-tree drift after Bash. The Bash call has already completed, so
# every response is notification or stop feedback rather than a permission gate.

set -u

emit_post_feedback() {
    local summary=$1 detail=$2

    jq -cn --arg summary "$summary" --arg detail "$detail" '{
        continue: true,
        systemMessage: $summary,
        hookSpecificOutput: {
            hookEventName: "PostToolUse",
            additionalContext: $detail
        }
    }'
}

valid_common_envelope() {
    local expected_exit_code=$1

    jq -e --argjson expected_exit_code "$expected_exit_code" '
        (type == "object") and
        (.verb == "drift") and
        (.status | type == "string") and
        (.exit_code == $expected_exit_code) and
        (.reservations | type == "array") and
        (.blocked_by | type == "array") and
        (.message | type == "string") and
        (.payload | type == "object") and
        (.payload.kind | type == "string") and
        (.payload.alerts | type == "array")
    ' <<<"$command_output" >/dev/null 2>&1
}

valid_no_facts_response() {
    local expected_exit_code=$1 expected_status=$2

    valid_common_envelope "$expected_exit_code" && jq -e --arg expected_status "$expected_status" '
        (.status == $expected_status) and
        (.reservations | length == 0) and
        (.blocked_by | length == 0) and
        (.payload.kind == "no_facts")
    ' <<<"$command_output" >/dev/null 2>&1
}

valid_drift_envelope() {
    local expected_exit_code=$1

    valid_common_envelope "$expected_exit_code" && jq -e '
        def nonempty_string: type == "string" and length > 0;
        def string_array: type == "array" and all(.[]; nonempty_string);
        def valid_publication:
            (type == "object") and
            ((.status == "published") or
             (.status == "unavailable" and (.diagnostic | nonempty_string)));
        def valid_acquisition:
            (type == "object") and
            (.kind == "appended" or .kind == "widened" or .kind == "already_held") and
            (.reservation_id | nonempty_string) and
            (.coordination_run_id | nonempty_string) and
            (.phase_start_head | nonempty_string) and
            (.marker_publication | valid_publication) and
            (.session_mapping_publication | valid_publication);
        def valid_file_scope:
            (type == "object") and
            (.path | nonempty_string) and
            (.kind == "file");
        def valid_file_scope_set:
            (type == "array" and length > 0) and all(.[]; valid_file_scope);
        def valid_head:
            (type == "object") and
            ((.kind == "branch" and (.full_ref | nonempty_string) and (.head | nonempty_string)) or
             (.kind == "detached" and (.head | nonempty_string)));
        def valid_source:
            (type == "object") and
            ((.kind == "work_plan" and (.plan | nonempty_string) and (.phase | nonempty_string)) or
             (.kind == "explicit" and (has("plan") | not) and (has("phase") | not)) or
             (.kind == "first_touch" and (has("plan") | not) and (has("phase") | not)));
        def valid_purpose:
            (type == "object") and
            ((.kind == "explained" and (.explanation | nonempty_string)) or
             (.kind == "not_provided_by_caller"));
        def valid_activity:
            (type == "object") and
            (.status == "active" or .status == "quiet") and
            (.last_activity_at | nonempty_string);
        def valid_scope:
            (type == "object") and
            (.path | nonempty_string) and
            (.kind == "file" or .kind == "tree");
        def valid_conflict:
            (type == "object") and
            (.reservation_id | nonempty_string) and
            (.reservation_revision | type == "number" and floor == .) and
            (.overlap_scope_revision | type == "array" and length > 0) and
            all(.overlap_scope_revision[]; valid_scope) and
            (.holder_worktree_id | nonempty_string) and
            (.holder_run_id | nonempty_string) and
            (.head_snapshot | valid_head) and
            (.source | valid_source) and
            (.purpose | valid_purpose) and
            (.overlapping_scopes | type == "array" and length > 0) and
            all(.overlapping_scopes[]; valid_scope) and
            (.claimed_at | nonempty_string) and
            (.activity | valid_activity);
        def valid_protection:
            (type == "object") and
            ((.status == "not_acquired") or
             (.status == "acquired" and
                (.acquisition | valid_acquisition) and
                (.scopes | valid_file_scope_set)));
        def valid_widening:
            (type == "object") and
            ((.status == "not_needed") or
             (.status == "attributed" and (.reservation_id | nonempty_string)) or
             (.status == "first_touch_claimed" and
                (.acquisition | valid_acquisition) and
                (.scopes | valid_file_scope_set)) or
             (.status == "post_write_incursion" and
                (.paths | string_array and length > 0) and
                (.conflicts | type == "array" and length > 0) and
                all(.conflicts[]; valid_conflict) and
                (.protection | valid_protection)) or
             (.status == "ambiguous" and (.candidates | string_array and length > 0) and (.paths | string_array and length > 0)) or
             (.status == "coordination_run_required" and (.paths | string_array and length > 0)));
        def valid_effect:
            (type == "object") and
            ((.kind == "widened" and (.added_scopes | type == "array" and length > 0) and
                all(.added_scopes[]; (type == "object") and (.path | nonempty_string) and (.kind == "file" or .kind == "tree"))) or
             (.kind == "incursion" and (.incident_id | nonempty_string) and
                (.foreign_reservation_ids | string_array and length > 0) and (.paths | string_array and length > 0)) or
             (.kind == "collision" and (.foreign_reservation_ids | string_array and length > 0) and
                (.paths | string_array and length > 0)));
        def valid_result:
            (type == "object") and (.reservation_id | nonempty_string) and
            ((.status == "unchanged") or
             (.status == "changed" and (.effects | type == "array" and length > 0) and all(.effects[]; valid_effect)));
        def has_effect($kind): any(.payload.data.results[]?; .status == "changed" and any(.effects[]?; .kind == $kind));
        def attribution_required:
            (.payload.data.widening.status == "ambiguous" or
             .payload.data.widening.status == "coordination_run_required");
        def expected_status:
            if .payload.data.widening.status == "post_write_incursion" or has_effect("incursion") then "incursion"
            elif has_effect("collision") then "drift_collision"
            elif .payload.data.widening.status == "first_touch_claimed" or has_effect("widened") then "widened"
            elif attribution_required then "drift_attribution_required"
            else "clear"
            end;
        def expected_exit:
            if .payload.data.widening.status == "post_write_incursion" or has_effect("incursion") or has_effect("collision") or attribution_required then 1 else 0 end;
        (.payload.kind == "drift") and
        (.payload.data | type == "object") and
        (.payload.data.comparison == "cheap_delta" or
         .payload.data.comparison == "full_phase_start" or
         .payload.data.comparison == "full_phase_start_fallback") and
        (.payload.data.widening | valid_widening) and
        (.payload.data.results | type == "array") and
        all(.payload.data.results[]; valid_result) and
        (.status == expected_status) and
        (.exit_code == expected_exit)
    ' <<<"$command_output" >/dev/null 2>&1
}

typed_drift_feedback() {
    jq -r '
        [
            (.payload.data.results[]? as $result |
                select($result.status == "changed") |
                $result.effects[] |
                if .kind == "widened" then
                    "AUTO-WIDEN: reservation \($result.reservation_id) now covers \([.added_scopes[] | .kind + ":" + .path] | join(", "))"
                elif .kind == "incursion" then
                    "INCURSION: reservation \($result.reservation_id) entered \(.paths | join(", ")), held by \(.foreign_reservation_ids | join(", ")); incident \(.incident_id). STOP. Resolve with `cargo-berth resolve \($result.reservation_id) --incursion \(.incident_id)` before making more changes."
                else
                    "COLLISION: reservation \($result.reservation_id) could not widen to \(.paths | join(", ")) because \(.foreign_reservation_ids | join(", ")) now holds the path. STOP and resolve the overlap before making more changes."
                end),
            (.payload.data.widening |
                select(.status == "first_touch_claimed") |
                "FIRST-TOUCH CLAIM: this write acquired reservation \(.acquisition.reservation_id) via \(.acquisition.kind); it now covers \([.scopes[] | .kind + ":" + .path] | join(", "))."),
            (.payload.data.widening |
                select(.status == "post_write_incursion") |
                "POST-WRITE INCURSION: changed paths \(.paths | join(", ")) are held by foreign reservations \([.conflicts[].reservation_id] | join(", ")). The write already happened. STOP and resolve the incursion before making more changes. " +
                if .protection.status == "acquired" then
                    "First-touch reservation \(.protection.acquisition.reservation_id) now protects the free paths \([.protection.scopes[] | .kind + ":" + .path] | join(", ")); acquisition kind \(.protection.acquisition.kind)."
                else
                    "Every observed path had a foreign holder; nothing was reserved."
                end),
            (.payload.data.widening |
                select(.status == "ambiguous") |
                "DRIFT ATTRIBUTION REQUIRED: paths \(.paths | join(", ")) match candidate reservations \(.candidates | join(", ")); every reported incursion and collision above is real. STOP, then attribute the paths with `cargo-berth drift --reservation <id> --json`."),
            (.payload.data.widening |
                select(.status == "coordination_run_required") |
                "DRIFT ATTRIBUTION REQUIRED: no coordination run identified the reservation for paths \(.paths | join(", ")); every reported incursion and collision above is real. STOP, then attribute the paths with `cargo-berth drift --reservation <id> --json`.")
        ] | join("\n")
    ' <<<"$command_output"
}

typed_drift_feedback_state() {
    jq -r '
        def has_effect($kind): any(.payload.data.results[]?; .status == "changed" and any(.effects[]?; .kind == $kind));
        def attribution_required:
            (.payload.data.widening.status == "ambiguous" or
             .payload.data.widening.status == "coordination_run_required");
        if .payload.data.widening.status == "post_write_incursion" or has_effect("incursion") or has_effect("collision") or attribution_required then
            "ImmediateStop"
        else
            "WideningNotification"
        end
    ' <<<"$command_output"
}

# Invoked by the command-capture trap.
# shellcheck disable=SC2329
cleanup_command_capture() {
    rm -f "$command_stdout_file"
}

harness_session_id=
payload_working_directory=
if ! hook_fields=$(jq -er '
    def nonempty_string: type == "string" and length > 0;
    select(.tool_name == "Bash") |
    select(.session_id | nonempty_string) |
    (if (.cwd? | nonempty_string) then .cwd else "" end) as $cwd |
    @sh "harness_session_id=\(.session_id) payload_working_directory=\($cwd)"
' 2>/dev/null); then
    emit_post_feedback 'cargo-berth rejected an invalid PostToolUse payload.' "STOP: berth_post_bash.sh requires valid JSON, tool_name Bash, and a non-empty session_id. Run \`cargo-berth drift --reservation <id> --json\` by hand."
    exit 0
fi
eval "$hook_fields"

if [ -z "$payload_working_directory" ]; then
    payload_working_directory=$PWD
fi

if [ ! -d "$payload_working_directory" ]; then
    emit_post_feedback 'cargo-berth could not inspect this Bash call.' "STOP: hook working directory $payload_working_directory does not exist."
    exit 0
fi

command_stdout_file=$(mktemp "${TMPDIR:-/tmp}/cargo-berth-post-bash.XXXXXX") || {
    emit_post_feedback 'cargo-berth could not inspect this Bash call.' 'STOP: the hook could not create its stdout capture file.'
    exit 0
}
trap cleanup_command_capture EXIT HUP INT TERM

# Both environment values are semantic inputs: the session mapping identifies
# the run, while POST_COMMIT selects reporting across every active local reservation.
if (
    cd "$payload_working_directory" || exit 5
    CARGO_BERTH_SESSION_ID=$harness_session_id CARGO_BERTH_POST_COMMIT=1 \
        cargo-berth drift --json
) >"$command_stdout_file" 2>/dev/null; then
    command_exit_code=0
else
    command_exit_code=$?
fi
command_output=$(<"$command_stdout_file")

case "$command_exit_code" in
    0|1)
        if ! valid_drift_envelope "$command_exit_code"; then
            emit_post_feedback 'cargo-berth rejected an untrusted drift response.' "STOP: drift exit $command_exit_code did not agree with its typed JSON envelope."
            exit 0
        fi
        drift_feedback=$(typed_drift_feedback)
        [ -n "$drift_feedback" ] || exit 0
        drift_feedback_state=$(typed_drift_feedback_state)
        case "$drift_feedback_state" in
            ImmediateStop)
                feedback_summary='cargo-berth detected drift that requires an immediate stop.'
                ;;
            WideningNotification)
                feedback_summary='cargo-berth widened this worktree reservation footprint.'
                ;;
            *)
                emit_post_feedback 'cargo-berth rejected an untrusted drift response.' 'STOP: validated drift output produced an unknown feedback state.'
                exit 0
                ;;
        esac
        emit_post_feedback "$feedback_summary" "$drift_feedback"
        ;;
    4)
        if valid_no_facts_response 4 unconfigured; then
            exit 0
        fi
        if valid_no_facts_response 4 ledger_unreadable; then
            ledger_detail=$(jq -r '.message' <<<"$command_output")
            emit_post_feedback 'cargo-berth could not read the reservation ledger after Bash.' "$ledger_detail"
        else
            emit_post_feedback 'cargo-berth rejected an untrusted drift response.' 'STOP: drift exited 4 without a valid unconfigured or ledger-unreadable envelope.'
        fi
        ;;
    5)
        if valid_no_facts_response 5 invalid_input; then
            selection_detail=$(jq -r '.message' <<<"$command_output")
            emit_post_feedback 'cargo-berth could not select or validate the drift reservation.' "$selection_detail Run \`cargo-berth drift --reservation <id> --json\` by hand."
        else
            emit_post_feedback 'cargo-berth rejected an untrusted drift response.' 'STOP: drift exited 5 without a valid invalid-input envelope.'
        fi
        ;;
    6)
        if valid_no_facts_response 6 contention; then
            contention_detail=$(jq -r '.message' <<<"$command_output")
            emit_post_feedback 'cargo-berth exhausted its ledger-lock deadline after Bash.' "$contention_detail The engine already spent its single 10-second retry budget; it was not invoked again."
        else
            emit_post_feedback 'cargo-berth rejected an untrusted drift response.' 'STOP: drift exited 6 without a valid contention envelope.'
        fi
        ;;
    *)
        emit_post_feedback 'cargo-berth returned an unreachable drift exit.' "STOP: drift exited $command_exit_code."
        ;;
esac

exit 0
