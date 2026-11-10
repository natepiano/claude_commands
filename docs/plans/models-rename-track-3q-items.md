# Phase 3q items routed from 3p review

Source: the 3p simplify review, pass 1, 2026-11-04. Line numbers are at the 3p review tree; find each site again before editing.
Items marked `held-for-decision` are not settled yet. Settle each under DecisionEconomy at 3p's close; a design choice goes to sdlc.
Also from 3p: F046 and F048 are superseded in 3p by F058 and F059 (their defect parts); close them at 3q. F048's rename part stays: Its enum rename (`DISPATCH_DEFERRED` vs stored `sending`, and a separate "no record" value) is 3q work.

## F006 (S1), routed from 3p repair round 9

1. **`scripts/production/worker_admission.py:346`, `:398-400`** · `in-phase`
   - Change: Make `dispatch` a `Callable[[PublishedWorker], SwitchState] | None`. Remove the `SwitchState` alternative and the `callable()` test. With `None`, write no state after publication.
   - Reason: Codex passes `None` (`codex_mesh.py:4238`). The intent is `prepared` before creation (`:390`), and `_start_turn` writes `sending` before submit (`codex_mesh.py:6170`). The write after publication is redundant.
   - Callers to update: `test_worker_admission.py:119`, `test_codex_mesh.py:2278`.

## F010 (S7)

7. **`scripts/agents/test_agent_bg.py:158`, `:209`** · `in-phase`
   - Change: Put `stopped.json` under `self.root` with a per-case name. Remove `if path != stopped`.
   - Reason: The exact folder check became a filtered check only because the test moved its own file into `parent`.

## F011 (S8)

8. **`scripts/production/worker_admission.py:112`, `:346`, `:378`** · `in-phase`
   - Change: Delete `PublishedWorker.state`. Write `return published` at both lines.
   - Reason: Nothing reads the field. It forces two extra constructor calls, and line 343 becomes live again.

## F012 (S9)

9. **`scripts/agents/codex_mesh.py:5625`, `scripts/production/worker_admission.py:367`** · `in-phase` (removes a copy)
   - Change: Add one `worker_admission.publish_created(allowed, identity, cleanup)` helper. Let `cleanup` return the detail text. Call it from both paths.
   - Reason: Two "publish, or release the created thread" paths now exist. The start path lost `; created thread stopped` and `; cleanup unproved:`.

## F013 (S11)

11. **`scripts/production/worker_fill.py:45`, `:50`, `:66`** · `in-phase`
    - Change: Add `action: str = "launch"` to `start`. Call `start(published.handoff, "attach")`. Delete `nonlocal started` and the write in `dispatch`.
    - Reason: `dispatch` sets a flag only to change a word that `start` prints.

## F014 (S12)

12. **`scripts/agents/agent_bg.sh:66`, `:79`, `:108`, `:152`, `:225`, `:253`** · `in-phase`
    - Change: Remove `ADMISSION_ACTIVE`. Set `SWITCH_PID=""` at line 79 and test it at lines 152 and 253. Make the `exec` at line 225 unconditional.
    - Reason: Line 66 is a dead write, and line 225 is always true. Keep the checks at lines 152 and 253, because `implement.sh:353` still passes `--attach`.

## F015 (S13)

13. **`scripts/production/worker_fill.py:75`, `scripts/agents/agent_bg.sh:70`** · `in-phase`
    - Change: Delete the `failed` print and the Bash `failed` branch. Pass `cleanup=lambda identity: None`.
    - Reason: EOF already carries this signal, and every failure of `publish_worker` maps to exit 2.

## F016 (S14)

14. **`scripts/agents/agent_bg.sh:63`, `:88`, `:225`** · `in-phase` (removes a copy)
    - Change: Put `exec {SWITCH_READ}<&- {SWITCH_WRITE}>&-` in one function.
    - Reason: Three sites hold one statement. The fix adds one line net.

## F017 (S15)

15. **`scripts/agents/agent_bg.sh:241`, `:91`, `:150`, `scripts/ask_a_friend/end_friend.sh:19`** · `in-phase`
    - Change: Write `publish_worker || exit 2`. Delete `"$ATTACH" == 0` from `close_switch`. Replace the unused `read` targets with `_`.
    - Reason: Each one is a leftover form or an unused name. `BG_ID` is set on the attach path only after publication.

## F018 (S16)

16. **`scripts/production/worker_admission.py:292`** · `in-phase` (removes a copy)
    - Change: Use `SwitchState.BACKEND_PUBLISHED` as the default.
    - Reason: The literal repeats the enum value.

## F019 (S17)

17. **`scripts/production/worker_admission.py:316`** · `in-phase` (removes a copy)
    - Change: Write `session_staff.worker_family(record, row.id) == "codex"`.
    - Reason: F013 asked for one family comparison.

## F020 (S18)

18. **`scripts/production/worker_admission.py:373`, `:376`** · `in-phase`
    - Change: Write `if retry:`.
    - Reason: `retry` implies `request` on both paths that set it.

## F021 (S19)

19. **`scripts/production/worker_fill.py:55`, `:61`, `scripts/production/worker_admission.py:235`** · `in-phase` (removes a copy)
    - Change: Add a lazy `worker_admission.transcript_entries(source, changed)` that yields `None` for a torn line. Read `prompt.read_text()` once, before the loop.
    - Reason: Two scan loops exist, and the prompt is read for each user line in the row lock. File iteration also stops a false torn line on U+2028.

## F022 (S20)

20. **`scripts/agents/codex_mesh.py:4230`, `:5677`** · `in-phase` (removes a copy)
    - Change: Add `_thread_id(result)` and call it at all 4 sites.
    - Reason: Deleting `_create_worker_backend` raised the expression from 3 sites to 4.

## F023 (S21)

21. **`scripts/production/worker_fill.py:68`, `scripts/agents/codex_mesh.py:6173`** · `in-phase` (removes a copy)
    - Change: Add `worker_admission.advance_dispatch(allowed, request, state)`. Merge the adjacent `if turn_id:` blocks at lines 6173 and 6176.
    - Reason: The same check and write appear at 2 sites, and the same condition appears twice in a row.

## F024 (S22)

22. **`scripts/agents/codex_mesh.py:4241`, `:6944`, `scripts/production/worker_fill.py:14`** · `in-phase`
    - Change: Delete the third blank line. Inline `record`. Merge the split imports.
    - Reason: These are noise.

## F025 (S23)

23. **`scripts/agents/codex_mesh.py:4348`** · `in-phase`
    - Change: Use `_as_str(_attr(args, "switch_request"))`.
    - Reason: The prompt is read 3 times and hashed twice per start.

## F026 (S24)

24. **`scripts/production/worker_fill.py:104`, `:108`** · `in-phase`
    - Change: Read one snapshot and pass it to `worker_admission.member`.
    - Reason: `member` parses the staff record twice, and `end_friend.sh` now uses this path.

## F027 (S25)

25. **`scripts/agents/agent_bg.sh:150`, `scripts/production/worker_fill.py:67`** · `in-phase`
    - Change: Print the native id and reference with `attach`. Skip `member` when the coordinator is active.
    - Reason: The coordinator already holds these facts. The attach retry pays one interpreter start and a backend scan in the row lock. Priority is low.

## F028 (S26)

26. **`scripts/production/worker_admission.py:309`** · `in-phase` (removes a copy, outside the phase)
    - Change: Add `StopResult` to `scripts/agents/stop_results.py`.
    - Reason: The union appears at 5 sites, and 4 of them are in `stop_resume.py`. Send those to their owner.

## F029 (S27)

27. **`scripts/production/worker_admission.py:249`, `:50`** · `in-phase` (removes a copy)
    - Change: Add `handoff_path(run, row)` and one prefix constant.
    - Reason: The path has 8 sites and the prefix has 4. The writer at line 251 and the parser at line 350 must stay in step.

## F030 (S28)

28. **`scripts/accounts/test_accounts.py:6358`** · `in-phase`
    - Change: Add `credits`, `write_error`, `refusal` and `anomalies` columns. Patch `_write_role` on every row. Delete the tautology at line 6398 and the `nullcontext` import.
    - Reason: Five branches on the case name across six rows make this table worse than the tests it replaced. It saves about 10 lines.

## F031 (S29)

29. **`scripts/accounts/test_accounts.py:6325`, `:6348`** · `in-phase`
    - Change: Drop the `(False, "codex1")` row, make `target` a literal, and delete line 6348.
    - Reason: The `codex2` dry-run row runs the same code. Line 6349 is already an exact check.

## F032 (S30)

30. **`scripts/accounts/test_accounts.py:6377`** · `in-phase` (removes a copy)
    - Change: Add a `use_credits` keyword to `write_note`. Use it here and at line 4954.
    - Reason: The note format is hand-written at 2 sites.

## F033 (S31)

31. **`scripts/direct/test_workers.py:633`, `:627`** · `in-phase`
    - Change: Replace both wrappers with `patch.object(worker_admission, boundary, wraps=..., side_effect=[AdmissionRefused(...), DEFAULT])`. Call `self.roster()` at lines 627, 632 and 653.
    - Reason: This saves about 14 lines and removes 3 copies of a fixture read.

## F034 (S32)

32. **`scripts/agents/test_codex_mesh.py:7878`** (also `:7983`, `:8130`, `:9113`, `:9947`, `:10251`, `:10275`, `:10371`, `:10752`) · `in-phase` (removes a copy)
    - Change: Add `run_worker(args, port, *, started_here=False)` to `MeshCommandFixture`.
    - Reason: The diff added 9 long lines in 2 spellings with 9 `pyright: ignore` comments.

## F035 (S33)

33. **`scripts/agents/test_codex_mesh.py:2137`, `:2130`, `:2150`** · `in-phase`
    - Change: Use the `flock` probe from `test_worker_admission.py:177`. Write `lambda _: streaming.set()`. Delete line 2150.
    - Reason: A child process and a 12-line program prove one free lock. `done()` is always true in a done callback.

## F036 (S34)

34. **`scripts/agents/test_codex_mesh.py:2217`, `:2223`, `:4788`** · `in-phase`
    - Change: Share the source setup with lines 2161-2165. Make the rows `(reason, change)` and pass `start` as a lambda. Seed only when `evidence != "absent"`.
    - Reason: These lines hold a setup copy, a `def` inside a loop, and 4 wasted roster writes.

## F037 (S35)

35. **`scripts/agents/test_agent_bg.py:147`, `:161`** · `in-phase` (removes a copy)
    - Change: Use `ShortUnixSocket() as gate` and `gate.bind_socket()`. Delete `import socket` and `import tempfile`.
    - Reason: `scripts/lib/socket_test_support.py` already owns this.

## F038 (S36)

36. **`scripts/agents/test_agent_bg.py:198`, `:224`, `:250`** · `in-phase`
    - Change: Use `launcher.kill()` as the `finally` body. Add one `clear_worker(row)` method.
    - Reason: The teardown appears twice, and `kill` does nothing after exit.

## F039 (S37)

37. **`scripts/agents/test_agent_bg.py:286`** · `in-phase`
    - Change: Run once with `FIXTURE_STOP_EXIT="1"`. Delete the loop, `try`, `finally` and `if`.
    - Reason: Row `"0"` asserts a strict subset of row `"1"`. This saves about 7 lines.

## F040 (S38)

38. **`scripts/agents/test_agent_bg.py:229`** · `in-phase`
    - Change: Keep rows `("", False)` and `("impl", True)`. Update the row count assertion.
    - Reason: `admit` discards the role, so 2 rows add at least 16 interpreter starts and prove nothing more.

## F041 (S39)

39. **`scripts/production/test_worker_admission.py:41`** · `in-phase`
    - Change: Keep the role loop for `admit`. Run the publish block once per `replaced` value.
    - Reason: The diff runs 12 publish and retire cycles where HEAD ran 2. Each `retire` runs a full `observe_workers` pass.

## F042 (S40)

40. **`scripts/production/test_worker_admission.py:45`, `:52`, `:150`, `:218`, `:220`, `:222`** · `in-phase`
    - Change: Delete the derivable assertions. Replace `cases` with `interrupted = family == "claude"`.
    - Reason: Each deleted check is implied by the next line or by `events`. This saves about 5 lines.

## F043 (S41)

41. **`scripts/production/test_worker_admission.py:43`** · `in-phase` (removes a copy)
    - Change: Add `StaffTestCase.staff_state()`.
    - Reason: The expression has 6 sites here and 63 lines across 9 files.

## F044 (S42)

42. **`scripts/production/test_worker_fill.py:21`, `:38`, `:78`** · `in-phase` (removes a copy)
    - Change: Delete the `resend` column. Add `refused = case == "unknown"`. Run `matched` and `unknown` through `worker_fill.main`. Add `input` to `StaffTestCase.script` for `absent`. Call `recorded_showrunner("demo", doc)`.
    - Reason: One fact has five forms, two interpreter starts add no proof, and line 78 repeats a helper.

## F047 (S46)

46. **`scripts/agents/test_agent_bg.py:187`** · `held-for-decision`
    - Change: Replace the ancestor walk with `_ = turn.recv(512)`. Send a fixed byte at line 38.
    - Reason: It saves about 6 lines and reads Linux-only `/proc` files with no guard. It also removes one direct F028 check. The coordinator exit check stays. Recommendation: take the change.

## F049 (S48)

48. **`scripts/production/worker_admission.py:313`, `scripts/accounts/accounts.py:2818`, `scripts/production/worker_fill.py:30`** · `held-for-decision`
    - Change: Accept or restore `source reference is unresolved`, `no run reference` and `ended before native identity proof`.
    - Reason: The refusal text changed with no order to change it. No test or script reads the old text. Recommendation: accept, but give `start` its own message.

## F050 (S49)

49. **`scripts/production/worker_admission.py:307`** · `held-for-decision`
    - Change: Move `release_source` to `stop_resume.py` and the `Source*` types to `stop_results.py`.
    - Reason: It is lifecycle logic. `accounts.py` imports the admission module only to stop a worker. The change is outside the phase.

## F051 (S50)

50. **`scripts/production/worker_admission.py:335`** · `held-for-decision`
    - Change: Add a one-row backend lookup in `staff.py`.
    - Reason: Codex starts on an existing row now pay one more `_found_backends` scan in the row lock. The change is outside the phase.

## F052 (S51)

51. **`scripts/production/staff.py:625`** · `held-for-decision`
    - Change: Check `_joined_operation` before `initial_snapshot()`.
    - Reason: The nested entry under `_run_worker` parses all rows only to join. The change is outside the phase.

## F053 (S52)

52. **`scripts/direct/workers.py:489`** · `held-for-decision`
    - Change: Keep the owner from `_ledger_for_run` and read one snapshot per lock hold.
    - Reason: Each candidate repeats the owner lookup in the ledger lock. Total work is still below HEAD. The change also moves the patch boundary of the new test.

## F054 (S53)

53. **`scripts/direct/workers.py:478`, `:498`** · `held-for-decision`
    - Change: Build a `roles` map once per `_resolve`.
    - Reason: `agents.jsonl` (2.1 MB) is parsed twice per roster entry in the lock. HEAD did this too.

## F055 (S54)

54. **`scripts/production/worker_admission.py:81`** · `held-for-decision`
    - Change: Merge `SourceUnresolved` and `SourceReleaseUnknown`.
    - Reason: No consumer tells them apart, and the merge saves about 5 lines. Recommendation: keep both, because Spec item 1 names them.

## F056 (S55)

55. **`scripts/ask_a_friend/end_friend.sh:23`** · `held-for-decision`
    - Change: Add a `worker_fill.py` verb that stops in process.
    - Reason: The Claude path gains one interpreter start. Recommendation: no change, because the path is not hot and F015 asked for the cut.

## F057 (S56)

56. **`scripts/agents/agent_bg.sh:100`, `:181`** · `held-for-decision`
    - Change: Run the launch setup between `coproc` and the first `read`.
    - Reason: Two Python starts wait for admission. The change alters failure order and adds work on the `unchanged` path. Recommendation: no change.

## Agent results

No test ran and no file changed. I read the code for items 1-13, 43-45 and 48. The other items rest on the agent reports and the diff.

- **reuse:** 9, 14, 16, 17, 19-21, 26, 27, 30-32, 34-36, 41, 42.
- **simplification:** 8, 11-13, 15, 16, 18, 21, 22, 28, 29, 31-34, 36, 37, 40, 42, 46, 54.
- **efficiency:** 1, 2, 19, 23-25, 33, 34, 38, 39, 42, 50-53, 55, 56.
- **altitude:** 1, 3, 4, 8-11, 43-45, 47-49.

I changed two agent proposals:
- **Item 1:** Altitude proposed that Codex always pass `BACKEND_PUBLISHED`. That write would replace a real `sending` on a retry and lose the retry check. With `None`, `switch_worker` writes nothing after publication.
- **Item 8:** Altitude wanted `state` kept so that `coordinate_claude` prints `unchanged` once. Simplification wanted the field deleted. I took the deletion, because the field has no reader and the stdout risk is not present today.

## Assertion and test changes

Skip, xfail and ignore: none. The gate 3 assertions stay. `test_moves.py` and `test_implement_launcher_claude.py` are not in the diff, and the matched, absent and unknown rows stay in `test_worker_fill.py`.

**Findings (the work order does not call for these)**

| Path | Test | Old text | New text | Item |
| --- | --- | --- | --- | --- |
| `scripts/production/test_worker_fill.py` | `test_admission_uses_current_unit_membership_and_is_unchanged_on_retry` | `assertEqual(worker_fill.admit(...), row)`; `assertEqual((output.getvalue(), errors.getvalue()), ("", ""))`; staff bytes and mtime unchanged | Removed, with no new home. `test_retired_unit_cannot_admit_a_worker` still proves `parent.id`. | 5 |
| `scripts/production/test_worker_fill.py` | `test_staff_id_admits_without_reading_malformed_fallback_run_state`, `..._an_unrelated_unreadable_pane_record` | `assertEqual(staff.find(...), [])` | `assertEqual(staff.find(...), before)` in `test_staff_id_admits_without_reading_unrelated_malformed_evidence` | 6 |
| `scripts/agents/test_codex_mesh.py` | `test_a_finished_follow_subscriber_cannot_turn_compaction_into_running_work` | `assertFalse(observer.is_alive())`; `assertEqual(failures, [])`; `block_compact_turn_completion = True`; a concurrent `consume` thread | Removed. The test runs `compact` first, sets `finished["launcher_pid"] = os.getpid()`, then reads the frames. | 4 |
| `scripts/agents/test_agent_bg.py` | `test_codex_to_claude_stops_the_source_and_publishes_before_the_transcript_opening` | `{... for path in parent.iterdir()}` | `{... for path in parent.iterdir() if path != stopped}` in `test_claude_admission_ends_at_dispatch_acceptance_or_cancellation` | 7 |

**Called for by the work order**

Old assertions that changed form:

| Path | Test | Old text | New text | Spec |
| --- | --- | --- | --- | --- |
| `scripts/direct/test_implement_launcher_claude_effort.py` | `ImplementLauncherWorkerTests` | `assertTrue(...current_session.startswith("abc12345-"))` | Removed. The session equality stays. | F034 |
| `scripts/production/test_worker_fill.py` | `test_uncertain_claude_dispatch_requires_readable_evidence_before_resending` | `assertRaisesRegex(AdmissionRefused, "delivery is unknown")` | Exit status 2 and `assertIn("delivery is unknown", result.stderr)` | F028 |
| same | same | `assertEqual(worker_fill.launch_claude(...), 0)` | `assertEqual(result.returncode, ...)` | F028 |
| same | same | `sent.read_text().splitlines()` against `["assignment dispatched"]` | `result.stdout.splitlines()` against the `attach`, `unchanged` or empty lines | F028 |
| same | same | `dispatch_record(allowed)["dispatch_state"]`, `"sending"` or `"accepted"` | `dispatch_record(allowed).state`, enum members | F016 |
| `scripts/agents/test_agent_bg.py` | `test_codex_to_claude_...` | `assertEqual(result.returncode, 0)`; `private["fresh-impl"]["dispatch_state"] == "accepted"` | `143 if cancel else 0`; `"sending" if cancel else "accepted"` | F028 |
| same | same | `sent["row"] == current.to_json()` through `staff.by_id(old.id)` | `sent["row"] == row.to_json()` through `staff.by_current_session(identity)` | 9 |
| `scripts/agents/test_agent_bg.py` | `test_fresh_claude_worker_publishes_its_registry_id_before_opening_work` | `assertEqual(len(calls), 1)`; `calls[0]` | `assertEqual(len(calls), count)`; `calls[-1]` | 9 |
| `scripts/production/test_worker_admission.py` | `test_switch_preserves_staff_graph_slot_folder_and_held_messages_before_dispatch` | `assertEqual(events, ["stop", "start", "dispatch"])` | `["stop", "start", *(["dispatch"] * (2 if interrupted else 1))]` | 9 |
| same | same | `assertEqual(events, ["stop", "start"])` in `dispatch` | Same list with a third `"dispatch"` on the retry | 9 |
| `scripts/production/test_worker_admission.py` | `test_codex_unit_admits_under_its_fixed_parent_id_after_thread_replacement` | `assertIsNone(staff.by_current_session(unit.id))` | `assertIsNone(staff.by_current_session(previous))` | 9 |
| same | `test_start_permission_creates_no_row_and_publication_uses_the_returned_thread` | Expected name `"test"`; `mock.patch.object(staff, "runtime_id", ...)` | Expected name `"new-worker"`; `CODEX_THREAD_ID` in the environment | 9 |

Tests removed and folded into a table, with their assertions kept (Spec item 9):

| Path | Old tests | New test |
| --- | --- | --- |
| `scripts/accounts/test_accounts.py` | `test_workers_swap_writes_role_before_stopping_fixed_workers`, `test_workers_swap_reports_each_failed_stop_and_keeps_new_role_for_pending_workers` | `test_workers_swap_selects_the_target_before_stops_and_keeps_pending_and_refused_workers` |
| same | `test_workers_swap_dry_run_names_target_and_preserves_roles_and_history`, `test_numbered_workers_target_selects_the_login_in_the_home_directory`, `test_workers_swap_accepts_default_real_shared_entries` | `test_numbered_workers_targets_keep_the_selected_home_with_real_or_shared_entries` |
| same | `test_workers_swap_role_write_failure_exits_one_before_interrupting_workers`, `test_workers_swap_refuses_paid_credits_when_the_target_setting_is_off`, `test_workers_swap_refuses_exhausted_usage`, `test_workers_swap_accepts_credits_when_a_window_is_used_up`, `test_workers_swap_reports_one_usage_anomaly_and_continues` | `test_workers_swap_checks_usage_and_credit_policy_before_changing_roles` |
| `scripts/agents/test_agent_bg.py` | `test_fresh_claude_worker_publishes_its_registry_id_before_opening_work`, `test_refused_claude_permission_starts_nothing_and_writes_no_worker_row`, `test_each_claude_launch_delivers_work_with_a_missing_or_invalid_task_role` | `test_fresh_claude_publication_precedes_work_for_each_task_role_and_parent_state` |
| same | `test_claude_publication_refusal_names_and_stops_only_the_created_backend_without_work`, `test_failed_claude_cleanup_keeps_the_unpublished_session_observable_and_dispatches_nothing` | `test_publication_refusal_stops_only_the_created_backend_and_reports_failed_cleanup` |
| `scripts/agents/test_codex_mesh.py` | `test_fork_supplies_and_copies_the_source_standing_instructions`, `test_fork_source_resume_supplies_standing_instructions`, `test_follow_resume_supplies_standing_instructions`, `test_compact_resume_supplies_standing_instructions`, `test_reset_resume_supplies_standing_instructions`, `test_follow_completion_keeps_the_standing_instructions_path`, `test_resume_without_standing_instructions_keeps_the_original_request`, `test_unreadable_standing_instructions_fail_before_any_turn` | `test_each_resume_uses_current_instructions_and_refuses_unreadable_content` |
| `scripts/production/test_worker_admission.py` | `test_valid_worker_roles_are_retained_by_read_only_start_permission`, `test_start_permission_creates_no_row_and_publication_uses_the_returned_thread`, `test_codex_unit_admits_under_its_fixed_parent_id_after_thread_replacement` | `test_permission_is_read_only_and_publication_uses_the_fixed_parent_for_each_task_role` |
| same | `test_a_published_switch_retries_dispatch_with_the_original_extract` | The `interrupted` row of `test_switch_preserves_staff_graph_slot_folder_and_held_messages_before_dispatch`. `starts == [identity]` became the single `"start"` in `events`. |
