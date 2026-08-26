#!/usr/bin/env bash

# Authorize Claude Code file-writing tools against cargo-berth's exact-file check.
# Bash writes are deliberately outside this hook and are observed after the fact.

set -u

refuse_edit() {
    printf 'cargo-berth refused this edit hook request: %s\n' "$1" >&2
    exit 2
}

emit_fail_open_notice() {
    local detail=$1

    jq -cn --arg detail "$detail" '{
        systemMessage: "cargo-berth could not establish edit safety; editing is allowed because ledger loss fails open.",
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            permissionDecisionReason: $detail
        }
    }'
}

emit_degraded_success_notice() {
    local reservation_id=$1 diagnostic=$2

    jq -cn --arg reservation_id "$reservation_id" --arg diagnostic "$diagnostic" '{
        systemMessage: ("cargo-berth authorized this edit with nonblocking degraded success. Reservation " + $reservation_id + " is durable, but the disposable session mapping is unavailable: " + $diagnostic),
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            permissionDecisionReason: ("Continue and name reservation " + $reservation_id + " explicitly from now on. Later edits fall back to CARGO_BERTH_RUN or the worktree marker.")
        }
    }'
}

lexically_normalize_absolute_path() {
    local candidate_path=$1
    local remaining_path component index
    local -a normalized_components

    case "$candidate_path" in
        /*) ;;
        *) return 1 ;;
    esac

    normalized_components=()
    remaining_path=${candidate_path#/}
    while :; do
        case "$remaining_path" in
            */*)
                component=${remaining_path%%/*}
                remaining_path=${remaining_path#*/}
                ;;
            *)
                component=$remaining_path
                remaining_path=
                ;;
        esac

        case "$component" in
            ''|.) ;;
            ..)
                if [ "${#normalized_components[@]}" -gt 0 ]; then
                    index=$((${#normalized_components[@]} - 1))
                    unset "normalized_components[$index]"
                fi
                ;;
            *) normalized_components[${#normalized_components[@]}]=$component ;;
        esac

        [ -z "$remaining_path" ] && break
    done

    if [ "${#normalized_components[@]}" -eq 0 ]; then
        printf '/\n'
    else
        local IFS=/
        printf '/%s\n' "${normalized_components[*]}"
    fi
}

find_repository_root_without_git() {
    local repository_probe=$1

    while :; do
        if [ -e "$repository_probe/.git" ]; then
            printf '%s\n' "$repository_probe"
            return 0
        fi
        [ "$repository_probe" = / ] && return 1
        repository_probe=${repository_probe%/*}
        [ -n "$repository_probe" ] || repository_probe=/
    done
}

valid_common_envelope() {
    local expected_exit_code=$1

    jq -e --argjson expected_exit_code "$expected_exit_code" '
        (type == "object") and
        (.verb == "check") and
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

valid_clear_check() {
    valid_common_envelope 0 && jq -e '
        def nonempty_string: type == "string" and length > 0;
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
        (.status == "clear") and
        (.blocked_by | length == 0) and
        (.payload.kind == "check") and
        (.payload.data.status == "clear") and
        (.payload.data.scopes | type == "array" and length > 0) and
        (.payload.data.acquisition | valid_acquisition) and
        (.reservations == [.payload.data.acquisition.reservation_id])
    ' "$command_stdout_file" >/dev/null 2>&1
}

valid_blocked_check() {
    valid_common_envelope 1 && jq -e '
        def nonempty_string: type == "string" and length > 0;
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
        (.status == "blocked_by_overlap") and
        (.reservations | length == 0) and
        (.payload.kind == "check") and
        (.payload.data.status == "blocked") and
        (.payload.data.scopes | type == "array") and
        (.payload.data.conflicts | type == "array" and length > 0) and
        all(.payload.data.conflicts[]; valid_conflict) and
        (.blocked_by == [.payload.data.conflicts[].reservation_id])
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

# Invoked by the command-capture trap.
# shellcheck disable=SC2329
cleanup_command_capture() {
    rm -f "$command_stdout_file" "$command_stderr_file" "$refusal_render_file"
    rmdir "$command_capture_directory" 2>/dev/null || true
}

tool_name=
supplied_edit_path=
payload_working_directory=
harness_session_id=
if ! hook_fields=$(jq -er '
    def nonempty_string: type == "string" and length > 0;
    .tool_name as $tool |
    (if $tool == "Edit" or $tool == "Write" then .tool_input.file_path
     elif $tool == "NotebookEdit" then .tool_input.notebook_path
     else error("unsupported tool")
     end) as $path |
    (if (.cwd? | nonempty_string) then .cwd else "" end) as $cwd |
    (if (.session_id? | nonempty_string) then .session_id else "" end) as $session |
    select(($tool | nonempty_string) and ($path | nonempty_string)) |
    @sh "tool_name=\($tool) supplied_edit_path=\($path) payload_working_directory=\($cwd) harness_session_id=\($session)"
' 2>/dev/null); then
    refuse_edit 'stdin was invalid JSON or did not name a supported tool and usable path'
fi
eval "$hook_fields"

case "$tool_name" in
    Edit|Write) edit_path_field=file_path ;;
    NotebookEdit) edit_path_field=notebook_path ;;
    *) refuse_edit "unsupported tool_name $tool_name" ;;
esac

# RepositoryEditTarget has three semantic states. Do not collapse outside-domain
# edits and invalid hook payloads into the same empty-path sentinel.
repository_edit_target_state=InvalidHookPayload
repository_relative_edit_path=

if [ -z "$supplied_edit_path" ]; then
    invalid_hook_payload_reason="$tool_name requires a non-empty tool_input.$edit_path_field"
else
    if ! normalized_edit_path=$(lexically_normalize_absolute_path "$supplied_edit_path"); then
        invalid_hook_payload_reason="tool_input.$edit_path_field must be an absolute path"
    else
        if [ -z "$payload_working_directory" ]; then
            payload_working_directory=$PWD
        fi

        if normalized_working_directory=$(lexically_normalize_absolute_path "$payload_working_directory") &&
           repository_root=$(find_repository_root_without_git "$normalized_working_directory"); then
            case "$normalized_edit_path" in
                "$repository_root"/*)
                    repository_edit_target_state=WithinRepository
                    repository_relative_edit_path=${normalized_edit_path#"$repository_root"/}
                    ;;
                *) repository_edit_target_state=OutsideCoordinationDomain ;;
            esac
        else
            repository_edit_target_state=OutsideCoordinationDomain
        fi
    fi
fi

case "$repository_edit_target_state" in
    OutsideCoordinationDomain) exit 0 ;;
    InvalidHookPayload) refuse_edit "$invalid_hook_payload_reason" ;;
    WithinRepository) ;;
    *) refuse_edit 'internal edit-target classification was invalid' ;;
esac

if [ -n "$harness_session_id" ]; then
    harness_session_identity_state=Present
else
    harness_session_identity_state=Absent
fi

command_capture_directory=$(mktemp -d "${TMPDIR:-/tmp}/cargo-berth-pre-edit.XXXXXX") ||
    refuse_edit 'could not create command capture files'
command_stdout_file=$command_capture_directory/stdout
command_stderr_file=$command_capture_directory/stderr
refusal_render_file=$command_capture_directory/refusal.json
trap cleanup_command_capture EXIT HUP INT TERM

run_check_once() {
    case "$harness_session_identity_state" in
        Present)
            (
                cd "$repository_root" || exit 5
                CARGO_BERTH_SESSION_ID=$harness_session_id cargo-berth check --json -- "file:$repository_relative_edit_path"
            )
            ;;
        Absent)
            (
                unset CARGO_BERTH_SESSION_ID
                cd "$repository_root" || exit 5
                cargo-berth check --json -- "file:$repository_relative_edit_path"
            )
            ;;
        *) return 5 ;;
    esac
}

if run_check_once >"$command_stdout_file" 2>"$command_stderr_file"; then
    command_exit_code=0
else
    command_exit_code=$?
fi

case "$command_exit_code" in
    0)
        valid_clear_check || refuse_edit 'check returned a malformed or inconsistent clear envelope'
        session_mapping_status=$(jq -r '.payload.data.acquisition.session_mapping_publication.status' "$command_stdout_file")
        if [ "$session_mapping_status" = unavailable ]; then
            reservation_id=$(jq -r '.payload.data.acquisition.reservation_id' "$command_stdout_file")
            mapping_diagnostic=$(jq -r '.payload.data.acquisition.session_mapping_publication.diagnostic' "$command_stdout_file")
            emit_degraded_success_notice "$reservation_id" "$mapping_diagnostic"
        fi
        exit 0
        ;;
    1)
        valid_blocked_check || refuse_edit 'check returned a malformed or inconsistent blocked envelope'
        if ! PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state \
            render-refusal --process-exit 1 --expected-verb check \
            <"$command_stdout_file" >"$refusal_render_file"; then
            refuse_edit 'the shared refusal classifier rejected the blocked envelope'
        fi
        refusal_markdown=$(jq -er '.state.rendered_markdown' "$refusal_render_file") ||
            refuse_edit 'the shared refusal classifier omitted rendered feedback'
        printf '%s\n' "$refusal_markdown" >&2
        exit 2
        ;;
    4)
        if valid_no_facts_response 4 unconfigured; then
            exit 0
        fi
        if valid_no_facts_response 4 ledger_unreadable; then
            fail_open_detail=$(jq -r '.message' "$command_stdout_file")
        else
            fail_open_detail="cargo-berth exited 4 without a trustworthy ledger envelope; the requested path was $repository_relative_edit_path"
        fi
        emit_fail_open_notice "$fail_open_detail"
        exit 0
        ;;
    5)
        valid_no_facts_response 5 invalid_input ||
            refuse_edit 'check returned a malformed or inconsistent invalid-input envelope'
        invalid_input_detail=$(jq -r '.message' "$command_stdout_file")
        refuse_edit "cargo-berth rejected payload path $supplied_edit_path (repository path $repository_relative_edit_path): $invalid_input_detail"
        ;;
    *)
        refuse_edit "check returned unreachable process exit $command_exit_code"
        ;;
esac
