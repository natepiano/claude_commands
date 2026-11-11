# Staff copies — notify, astra, production, usage

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Records that copy a staff fact keep the staff id only and read the fact from the staff row when they use it.

> **As-built disposition: amend** — `docs/as-built/session-notifier.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/usage-ledger.md`

## Delegation Context

- **Project:** `~/.claude` scripts — stale staff copies, owner_and_fix sections 3 (notify), 4 (astra), 7 (production) and 8 (usage).
- **Project started:** 2026-11-11T23:00:00+00:00
- **Stack:** Python 3.11+ (basedpyright strict), zsh (`scripts/message/notifier.sh`).
- **Rule (sdlc):** read the fact from the staff row by its fixed id; delete the copy; old data gets one migration at deploy.
- **Staff API:** `scripts/production/staff.py` — `by_id(staff_id) -> StaffRow | None`, `snapshot() -> StaffSnapshot`, `StaffSnapshot.by_id`, `StaffSnapshot.parent_of(row)`. `StaffRow` gives `name`, `current_session`, `doc`, `home` and `worktree`. `current_session_target(target)` resolves a saved staff address at delivery.
- **Deploy migration:** each phase that changes saved data writes one conversion program outside the repository, in the director scratchpad. The checkpoint notice names it and its run steps for natedev and the Mac. The program runs once at deploy and is then deleted. The main code has no old-shape reader.
- **Ownership:** hierarchies owns section 1 (display read-backs), section 9 and every site in both lists. Tell hierarchies before an edit to `scripts/message/*` or `scripts/hooks/*`. Tell builds before an edit to `scripts/copies/*`.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest <modules>`.
- **Lint:** `uvx basedpyright <changed Python files>` — `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Do not change `config/copies.conf`, `config/direct.conf` or any permission setting.
  - The user adds no checks: add no guard, refusal or scan that a Work Order does not name.
  - Processes clean up after themselves.
  - A fact read from staff is read when it is used, never saved again.

## Phases

### Phase 1 — Usage runs keep no state or branch  · status: done

**Depends on:** none.

#### Work Order

**Goal:** `usage.db` table `runs` has no `state` and no `branch` column. No code reads either column.

**Spec:**

1. `scripts/usage/collect.py` `SCHEMA`: remove `branch` and `state` from `CREATE TABLE IF NOT EXISTS runs`.
2. `save_history_event`: the `run_started` INSERT names its columns and writes no `branch` and no `state`. `run_finished` updates `finished_at` only. Phase and pass finishes keep `SET state = ?, finished_at = ?`.
3. Do not add a `migrate_store` step or bump `PRAGMA user_version`. The deploy program changes old databases.
4. Deploy program (director scratchpad, not the repository): `ALTER TABLE runs DROP COLUMN branch` and `ALTER TABLE runs DROP COLUMN state` on `~/.local/state/usage-model/usage.db`, with the collector timer stopped. It does nothing when the columns are gone.
5. Add no other guard, refusal or scan.

**Files:**
- `scripts/usage/collect.py` — schema and history writes.
- `scripts/usage/test_outcomes.py` — schema and history rows.

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/usage/collect.py`.
- `test` — `scripts/usage/test_outcomes.py`. Change the rows that assert `runs.branch` or `runs.state` (`test_outcome_schema_keeps_identity_and_source_event_without_finding_details`, `test_history_rows_use_file_run_id_creation_event_and_later_status`, `test_history_finish_tail_waits_for_newline_then_updates_existing_rows`, `test_mac_history_updates_incrementally_and_unchanged_pass_sends_no_outcomes`, `test_mac_history_without_newline_waits_then_finishes_run`). A finished run keeps its `finished_at`.

**State:** `usage.db` schema; deploy program above.

**Constraints from prior phases:** none.

**Acceptance gate:**
1. The unittest line passes for `scripts.usage.test_outcomes` and `scripts.usage.test_collect`.
2. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.

### Phase 2 — Production records keep staff ids  · status: todo

**Depends on:** none.

#### Work Order

**Goal:** `add_showrunner.json`, `review-paused/*.json` and `wrap_release.json` keep staff ids. Session, document, home and recipient address come from the staff row.

**Spec:**

1. `scripts/production/add_showrunner.py` `published`: the receipt has no `session_id`. A receipt for an owner with no staff row carries `doc` and `config_dir` as its creation claim. A receipt for a staff owner carries neither. After `ensure_staff_showrunner` admits the owner, `published` rewrites the receipt without the claim.
2. `scripts/production/completion_receipt.py` `read_launch_receipt`: an unpublished receipt is one whose `owner` has no staff row. Its native session is `owner`.
3. `add_showrunner.py` readers: an owned receipt takes session from `row.current_session`, document from `row.doc` and home from `row.home`. Only an unpublished receipt reads `doc` and `config_dir`. `ShowrunnerLaunch.from_receipt` takes the receipt variant, not `session_id`.
4. `scripts/production/review_pause.py`: the record path is `review-paused/<owner staff id>.json`. `pause`, `status` and `resume` select the owner by production name through staff, then use the id. Output shows the name.
5. `scripts/hooks/showrunner_footer.py` `review_pause_path` and `scripts/hooks/conversation_pause.py` `_review_names` read the id-keyed file. Tell hierarchies before this edit.
6. `scripts/production/showrunner_tree.py` `edges_to_release` and `scripts/production/production_lifecycle.py` `save_release`: the capture stores recipient staff ids. `release_edges` resolves each current row at delivery through `showrunners.current_address`.
7. Deploy program: rename `~/.local/state/showrunner/review-paused/<name>.json` to `<owner id>.json`; strip `session_id` from any `add_showrunner.json`, and `doc` and `config_dir` from one with a staff owner; convert any `wrap_release.json` names to ids. natedev holds 1 pause file and 0 of the others.
8. Add no other guard, refusal or scan.

**Files:**
- `scripts/production/add_showrunner.py` — receipt writer and readers.
- `scripts/production/completion_receipt.py` — receipt decode.
- `scripts/production/review_pause.py` — pause key.
- `scripts/hooks/showrunner_footer.py` — pause reader.
- `scripts/hooks/conversation_pause.py` — pause reader.
- `scripts/production/showrunner_tree.py` — release capture.
- `scripts/production/production_lifecycle.py` — release save and delivery.
- `scripts/production/test_add_showrunner.py` — receipt rows.
- `scripts/production/test_completion_receipt.py` — receipt rows.
- `scripts/production/test_production_lifecycle.py` — release rows.
- `scripts/production/test_tidy_wrapped.py` — pause cleanup rows.
- `scripts/production/test_review_pause.py` — pause rows (new file).
- `scripts/production/test_showrunner_parent.py` — wrap release rows.
- `scripts/production/test_showrunner_tree.py` — release capture rows.
- `scripts/hooks/test_showrunner_footer.py` — pause reader rows.
- `scripts/hooks/test_conversation_pause.py` — pause reader rows.

**Workers:** 1 writer + 1 tester.
- `impl` — the source files above.
- `test` — the test files above. Rows: a dead owned launch recovers with the session and home from staff after a rename; an unpublished launch recovers from `owner`; a pause by name writes and reads the id file; a wrap release delivers to a recipient's current session after its session changes.

**State:** the three record shapes; deploy program above.

**Constraints from prior phases:** none.

**Acceptance gate:**
1. The unittest line passes for the nine test modules above.
2. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.

### Phase 3 — Scheduled prompts are built at delivery  · status: todo

**Depends on:** none.

#### Work Order

**Goal:** no `prompt.txt` and no `PROMPT_FILE`. A tick builds the showrunner prompt from the timer owner's staff row when it sends.

**Spec:**

1. `scripts/production/update_registration.py`: add a `prompt --owner <staff id>` command. It prints `scheduled_prompt(minutes, production)` for the owner's current row. Minutes come from the production doc schedule, as in `adopt_moved_state`.
2. `register` writes `PROMPT=scheduled` in the timer conf, not `PROMPT_FILE`, and writes no `prompt.txt`. `adopt_moved_state` no longer rewrites a prompt file.
3. `scripts/message/notifier.sh`: `write_conf` and `cmd_new` carry `PROMPT=scheduled`. `instance_tick` runs the `prompt --owner "${conf[TARGET]}"` command and sends its output. A failed build skips the tick with one stderr line. Tell hierarchies before this edit.
4. `commands/showrunner/report_interval.md`: remove the prompt-file edit step. `docs/as-built/session-notifier.md`: describe the delivery-time prompt and the owner-id target.
5. `scripts/production/fake_showrunner.py` `write_timer` matches the new conf.
6. Deploy program: in each `~/.local/state/notifier/showrunner-*/conf`, replace `PROMPT_FILE=...` with `PROMPT=scheduled`; delete every `~/.local/state/showrunner/*/prompt.txt`, the 3 with no staff row included. natedev holds 5 confs and 8 prompt files.
7. Add no other guard, refusal or scan.

**Files:**
- `scripts/production/update_registration.py` — `prompt` command and registration.
- `scripts/production/fake_showrunner.py` — test timer fixture.
- `scripts/message/notifier.sh` — conf key and tick delivery.
- `commands/showrunner/report_interval.md` — no prompt-file edit.
- `docs/as-built/session-notifier.md` — delivery-time prompt.
- `scripts/message/test_notifier.py` — tick rows.
- `scripts/production/test_update_registration.py` — registration rows.
- `scripts/production/test_update_registration_log.py` — state move rows.
- `scripts/production/test_update_registration_commands.py` — prompt command rows.

**Workers:** 1 writer + 1 tester.
- `impl` — the source and doc files above.
- `test` — the test files above. Rows: a tick after a showrunner rename sends the new name; a tick after a doc move sends the new path; no `prompt.txt` exists after registration.

**State:** notifier conf key; deploy program above.

**Constraints from prior phases:** none.

**Acceptance gate:**
1. The unittest line passes for the four test modules above.
2. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.

### Phase 4 — Astra design records keep staff ids  · status: todo

**Depends on:** none.

#### Work Order

**Goal:** an opened design role keeps its worker staff id and no name. A staff-owned checkout keeps the owning unit id and no path. The legacy `holder` object is gone.

**Spec:**

1. `scripts/astra/design_roles.py`: a role with no worker id keeps `name` as its creation claim. A role with a worker id saves no `name`; `_prompt`, `dispatch_turn`, `recover`, `end` and `list_roles` read the name from the worker's staff row.
2. `repository` and `binding.repository`: when the checkout equals the owning unit's staff worktree at open, save `repository_owner: <unit id>` and no path. Readers resolve the path from that row's `worktree`. Any other checkout keeps its path.
3. `scripts/message/roster.py` `_durable_roles` and `worker_shown_name` read the name and path the same way. Tell hierarchies before this edit.
4. Deploy program: delete the `holder` object from each `~/.local/state/astra/design-roles/*/design.json`; drop `name` from roles with a worker id; convert a staff-owned `repository` to `repository_owner`. natedev holds 1 design file with 1 holder and 2 unopened roles.
5. Add no other guard, refusal or scan.

**Files:**
- `scripts/astra/design_roles.py` — design record shape and readers.
- `scripts/message/roster.py` — durable role readers.
- `scripts/astra/test_design_roles.py` — design rows.
- `scripts/hooks/test_berth_hook.py` — patch check rows.
- `scripts/message/test_roster.py` — roster rows.

**Workers:** 1 writer + 1 tester.
- `impl` — the source files above.
- `test` — the test files above. Rows: a worker rename shows the new name in the prompt and the role list; a unit worktree move resolves the new checkout path; an independent checkout keeps its path.

**State:** design record shape; deploy program above.

**Constraints from prior phases:** none.

**Acceptance gate:**
1. The unittest line passes for the three test modules above.
2. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.
