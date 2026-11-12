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

#### As-built

- `usage.db` table `runs` has the columns `machine`, `run_id`, `started_at`, `plan_doc`, `worktree`, `finished_at` and `source_event`. It has no `state` and no `branch` column.
- `HISTORY_FIELDS` gives `run_started` the fields `plan_doc` and `worktree`, and gives `run_finished` no fields.
- The `run_started` INSERT names its six columns. `run_finished` sets only `finished_at`. Phase and pass finishes keep `SET state = ?, finished_at = ?`.
- `validate_history_fields` checks an open status only for `phase_finished` and `pass_finished`.
- The new code writes to an old database that still has the two columns: they are nullable and stay NULL. A one-off deploy program outside the repository drops them.

**Files:**
- `scripts/usage/collect.py` — `HISTORY_FIELDS`, `SCHEMA` and the history event writers.
- `scripts/usage/test_outcomes.py` — schema and history row tests.

**Ruled out:** a collector schema migration or `PRAGMA user_version` bump — old data changes once at deploy.

### Phase 2 — Production records keep staff ids  · status: done

#### As-built

- `add_showrunner.json` has no `session_id`. A new receipt carries `doc` and `config_dir` as its creation claim. `finish_publication` saves `config_dir` as the row home with `staff.set_home`, then rewrites the receipt without `doc` and `config_dir`. The first launch and a rerun of an owned receipt that still holds the claim both run `finish_publication`, so a stop between admission and the home write recovers on the next run.
- `completion_receipt.read_launch_receipt`: a receipt whose `owner` has no staff row is unpublished, and its native session is `owner`. An owned receipt reads session, document and home from the staff row. `ShowrunnerLaunch.from_receipt` takes the receipt variant.
- `review_pause.py` keeps `review-paused/<owner staff id>.json`. `pause`, `status` and `resume` select the owner by production name through staff; output shows the name. `showrunner_footer.review_pause_path` and `conversation_pause._review_names` read the id-keyed file.
- `wrap_release.json` stores recipient staff ids in `former_parent`, `former_children` and `told`. `release_edges` reads each recipient's current name from staff at its own delivery, and reads the names again for the final report.
- A one-off deploy program outside the repository renames old pause files, strips the copied receipt fields and converts release names to ids.

**Files:**
- `scripts/production/add_showrunner.py` — receipt writer, `finish_publication` and readers.
- `scripts/production/completion_receipt.py` — receipt decode.
- `scripts/production/review_pause.py` — id-keyed pause record.
- `scripts/hooks/showrunner_footer.py`, `scripts/hooks/conversation_pause.py` — pause readers.
- `scripts/production/showrunner_tree.py` — release capture with ids.
- `scripts/production/production_lifecycle.py` — release save and per-delivery name lookup.
- `scripts/production/test_add_showrunner.py`, `test_completion_receipt.py`, `test_production_lifecycle.py`, `test_review_pause.py`, `test_showrunner_parent.py`, `test_showrunner_tree.py`, `scripts/hooks/test_conversation_pause.py` — rows for the three record shapes.

**Ruled out:** resolving all recipient names once before delivery — a rename during an earlier notice sends to a stale name.

### Phase 3 — Scheduled prompts are built at delivery  · status: done

#### As-built

- No `prompt.txt` and no `PROMPT_FILE`. `register` and `notifier.sh new` write `PROMPT=scheduled` in the showrunner timer conf. `adopt_moved_state` rewrites no prompt file.
- `update_registration.py prompt --owner <staff id>` prints `scheduled_prompt(minutes, production)` for the owner's current staff row. Minutes come from the production doc schedule. The prompt is the template text only, with no document-path line.
- `notifier.sh` `instance_tick`: for `PROMPT=scheduled`, it runs the `prompt --owner` command and sends its output. A failed build prints one stderr line and skips that tick.
- `codex_winddown.start_count` passes its TICK text with `--command` and writes no prompt file.
- `produce.md`, `report_interval.md` and `docs/as-built/session-notifier.md` describe the delivery-time prompt and the owner-id target.
- A one-off deploy program outside the repository converts each `showrunner-*/conf` to `PROMPT=scheduled` and deletes every `~/.local/state/showrunner/*/prompt.txt`.

**Files:**
- `scripts/production/update_registration.py` — `prompt` command and registration.
- `scripts/message/notifier.sh` — conf key and tick delivery.
- `scripts/production/codex_winddown.py` — count timer text by `--command`.
- `scripts/production/fake_showrunner.py` — test timer fixture.
- `commands/showrunner/produce.md`, `commands/showrunner/report_interval.md`, `docs/as-built/session-notifier.md` — delivery-time prompt.
- `scripts/message/test_notifier.py`, `scripts/production/test_codex_winddown.py`, `test_update_registration.py`, `test_update_registration_log.py`, `test_update_registration_commands.py` — tick, registration and prompt rows.

### Phase 4 — Astra design records keep staff ids  · status: done

#### As-built

- `scripts/astra/design_roles.py` holds the checkout as a tagged field: `UnitCheckout(owner)` or `IndependentCheckout`. When the checkout equals the caller unit's staff worktree, the record saves `repository_owner: <unit id>` and no path, in the design and in its binding. Any other checkout keeps its path.
- An owned design saves `document` relative to its checkout. Readers resolve the checkout from the owner row's current `worktree`, so a worktree move moves the document too. `held_document` resolves raw owned records the same way, so a damaged matching record still refuses.
- `rebind` to the current checkout keeps the recorded owner and binding for any caller. An absent owner row raises `RoleRefused`, which the roster's per-record handling reports as a warning.
- A role with a worker staff id saves no `name`. `instance_name` reads it from the worker's staff row for `_prompt`, `dispatch_turn`, `recover`, `end` and `list_roles`. A role with no worker id keeps `name` as its creation claim. The legacy `holder` object is gone.
- `scripts/message/roster.py` `_durable_roles` and `worker_shown_name` use `instance_name` and `design_repository`.
- A one-off deploy program outside the repository deletes `holder`, drops `name` from roles with a worker id, and converts a `repository` that equals a unit's staff worktree to `repository_owner` with a relative `document`.

**Files:**
- `scripts/astra/design_roles.py` — checkout types, record shape and readers.
- `scripts/message/roster.py` — durable role readers.
- `scripts/astra/test_design_roles.py`, `scripts/hooks/test_berth_hook.py`, `scripts/message/test_roster.py` — rename, checkout move, rebind, absent owner and prompt-count rows.
