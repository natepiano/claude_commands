# sdlc-devtools-tests

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Make the `~/.claude` repo tests run in every gate, faster, and with less CPU. Phase 1 fixes the gate's test list. The measured inventory and its phases come after Phase 1.

> **Production: sdlc** — unit `devtools-unit`; production doc `docs/plans/sdlc-production.md`

## Source

The user, through the sdlc showrunner, 2026-10-30: "it can also be where we look at making repo tests go faster and take less cpu". Phase 1: herdr through natedev and the showrunner, 2026-10-30. natedev agreed the `merge_gate.py` edit ("take it"; no open natedev branch touches it).

Known input for the inventory: `scripts/agents/test_codex_mesh.py` was killed at the 1800 s module limit under load (gate_merge77.log). It takes about 1251 s unloaded and 741 s in a later gate, and it grows with each mesh test. Check first if herdr's plan retires the mesh soon.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-10-30T11:40:41.136+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Key files:**
  - `scripts/production/merge_gate.py` — the merge gate: finds test modules, runs them under a memory cap, keeps the passed-tree ledger (`PassedTreeLedger`)
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — its fixture and tests
  - `docs/as-built/merge-gate-and-test-speed.md` — the gate's as-built
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only --full`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, `~/.cargo/bin`).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - All text uses Simplified Technical English (user, 2026-10-18).
  - One place for each thing: no second list of suites anywhere (user, 2026-10-30).

## Phases

### Phase 1 — The merge gate finds every test module itself  · status: todo
**Depends on:** none

#### Work Order

**Goal:** the merge gate runs every `scripts/**/test_*.py` module with no hand list, so a new test folder is in every gate from its first commit, and admits jobs by their measured memory, so a gate with no failed test is not red from the memory cap.

**Spec:**

- Cause: `TEST_SUITES` at `scripts/production/merge_gate.py:63` is a hand list. A folder with tests runs in a gate only when someone adds it, and `EXPECTED_SUITES` in `merge_gate_test_support.py` is a second copy. Eleven folders were never added, so 22 modules never ran in any gate: `review_links`, `exclude`, `hana_shot`, `nix_env`, `screen_record`, `settings`, `shot_report`, `validate_and_push`, `worktree_delete`, and the nested `scripts/berth/tests/` and `scripts/prioritize/tests/`.
- Once-run, 2026-10-30, the gate's way (cwd = repo, `unittest discover -s <folder> -p <file>`): 22 of 24 modules green, including `nightly_review/test_candidates.py` and `style/test_add_style.py`. Two were red:
  - `berth/tests/test_hook_rendering.py`: `ModuleNotFoundError: No module named 'berth'`. It checks the installed front end (`~/.cargo/bin/cargo-berth` and the hooks under `~/.claude/scripts`), not the checkout, and its setUpClass raises when nothing is installed. It is not a gate test.
  - `shot_report/test_transcripts.py`: 8 failures only when TMPDIR is inside a Claude scratchpad path. Under the gate's TMPDIR in `/dev/shm` all 90 pass. Sent to natedev's screenshot-unit; not this phase.
- `merge_gate.py`: delete `TEST_SUITES`. One function finds the modules: every `scripts/**/test_*.py` outside `__pycache__`, sorted. A module's suite is its first folder under `scripts/`. Every user of the list uses the found suite names instead: the passed-tree ledger `suites` field (written and compared as the sorted found names) and the per-suite report. The refusal "suites have no test modules" goes, because a suite now exists only through its modules. The refusal "the test list is empty" stays. A nested module runs as every module does now: `discover -s <its folder> -p <its file>`, cwd = repo.
- `merge_gate_test_support.py`: `EXPECTED_SUITES` and the `merge_gate.TEST_SUITES == EXPECTED_SUITES` check go. The fixture keeps the names of the folders it writes as fixture data. `test_merge_gate_ledger.py:323` patches `TEST_SUITES`; it uses the fixture's folders instead.
- New tests: a folder that no code names runs; a module in a nested `tests/` folder runs under its top folder's suite; a folder without `test_*.py` is no suite; a changed suite set does not reuse or certify an old ledger record.
- Rename `scripts/berth/tests/test_hook_rendering.py` to `scripts/berth/tests/check_installed_hooks.py`. Add one line to `scripts/berth/install/README.md` that tells how to run it after an install: `cd ~/.claude/scripts && python3 -m unittest berth.tests.check_installed_hooks`.
- Docs that state the list or its rule: `docs/as-built/merge-gate-and-test-speed.md` (:9, :52, :61, :171), `docs/as-built/long-lived-workers.md:452`, `docs/as-built/style-showrunner.md:43`, `docs/as-built/bevy-upgrade.md:441`.
- Cost: the ledger key changes one time, so the first gate after this change runs in full. The new modules add about 120 module-seconds (`hana_shot` 58 s, `screen_record` 36 s, the rest under 3 s each). A new test folder later changes the key again and forces one full run; that is correct, because the old record did not cover it.
- Memory cap (natedev, 2026-10-30): full gates go red at random on the sampled memory measure alone, with no failed test. Style's gates for phases 101 and 107 peaked at 4.039 and 4.048 GiB against the 4.000 GiB cap with 16 modules at once; builds' report 4b gate killed basedpyright at 4.012 GiB; resources' gates peaked at 4.028 and 4.029 GiB. This phase adds 22 modules, so the sum grows. Two causes, measured:
  - Admission learns module size during the run. `SampledRSSAdmission.headroom_mib` starts at 96 MiB in every run, so up to 16 modules start before the first sample with 96 MiB each and then grow to their real peaks (the largest is 0.52 GiB). Admission cannot stop a job that already runs.
  - The cap is smaller than the largest single job. basedpyright over `scripts` last peaked at 4065 MiB (`types_peak_mib.json`, 2026-10-30), so its reservation (× 1.2 = 4878 MiB) is over the 4096 MiB cap: it runs alone at the cap, and any growth kills it.
- Fix: every job reserves its own measured peak. The runner records each module's sampled peak under its `suite/module` key in the state dir, beside `types_peak_mib.json`, with the same read and save functions; a job killed for a limit records nothing. Every job reserves its last recorded peak × one margin, 1.2, one constant for modules and the types job. A module with no record reserves the largest recorded module peak × the margin, or 640 MiB (the measured largest module, 0.52 GiB × 1.2) when no module has a record. A new job starts when the latest aggregate sample + the remaining reservations of the running jobs + the new job's reservation ≤ the cap; with nothing running, one job always starts. The run-learned `headroom_mib` goes: one reservation rule covers every job.
- Cap: `DEFAULT_MEMORY_CAP_MIB` goes from 4096 to 6144 MiB, from the measure: the types job's 4878 MiB reservation plus room for modules beside it. A job over the cap is still killed, and a sampled peak over the cap still makes the run red.
- Memory docs: the `merge_gate.py` docstring (:4-7), the `--memory-cap-mib` help, and `docs/as-built/merge-gate-and-test-speed.md` Admission (:75) and its table (:195).
- Memory tests: a module reserves its recorded peak × the margin from its first admission, before any sample; a module with no record reserves the largest recorded module peak; with no record at all, 640 MiB; modules whose recorded peaks sum past the cap never run at once; a killed module records no peak; the default cap holds the types job's reservation from the recorded full-tree peak. The headroom tests in `test_merge_gate_suites_admission.py` change with the rule; each changed or removed assertion gets an `Assertion-Change:` line.

**Files:**
- `scripts/production/merge_gate.py`
- `scripts/production/merge_gate_test_support.py`
- `scripts/production/test_merge_gate.py`
- `scripts/production/test_merge_gate_ledger.py`
- `scripts/berth/tests/test_hook_rendering.py`
- `scripts/berth/tests/check_installed_hooks.py`
- `scripts/berth/install/README.md`
- `docs/as-built/merge-gate-and-test-speed.md`
- `docs/as-built/long-lived-workers.md`
- `docs/as-built/style-showrunner.md`
- `docs/as-built/bevy-upgrade.md`
- `docs/as-built/showrunner-automation.md`
- `scripts/production/test_merge_gate_holds.py`
- `scripts/production/test_merge_gate_suites_resources.py`
- `scripts/production/test_merge_gate_suites_reruns.py`
- `scripts/production/test_merge_gate_suites.py`
- `scripts/production/test_merge_gate_suites_admission.py`

**Workers:** `1 writer` — one small change in one module and its tests.

**State:** the passed-tree ledger `suites` field holds the found names; new `module_peak_mib.json` in the gate state dir holds each module's last sampled peak (written at run end with `save_measures`, beside `types_peak_mib.json`).

**Acceptance gate:** the merge gate tests pass; the checkout's own gate (`merge_gate.py --repo . --tests-only --full`) is green and its report names all 40 suites (the 29 listed today plus the 11 new) and its sampled peak memory is under the cap; basedpyright prints `0 errors, 0 warnings, 0 notes` on the changed Python files; no file in the repo lists suite names.

### Phase 2 — Every test uses the one hang guard  · status: todo
**Depends on:** Phase 1; models' checkpoint that moves the hang guard to `scripts/lib` and moves `scripts/agents` onto it (ETA 10:45 EDT, 2026-10-30)

Source: the sdlc showrunner, 2026-10-30. 106 test files under `scripts/` each define their own `TEST_HANG_GUARD_SECS` (105 at 300 s, `recorder_fixture.py` at 600 s), and many set their own short real-time limits that fail under load (the cause of the `agents/test_codex_mesh.py` red in Phase 1's gate). After models' checkpoint, one mechanical checkpoint from devtools: every other test imports the one guard; each short real-time limit goes onto the guard, or onto the shared `LifecycleTestClock` where time is the behavior under test; and a check refuses a new local copy. Owners are many; tell natedev which natedev-owned folders it touches. The Work Order is written when models sends the import form.

### Phase 3 — Close-route closes a route made before its phase record  · status: done
**Depends on:** none

#### As-built

- `scripts/history/routed.py` keys each route `<run id>:<phase instance id>:<finding id>` from its `finding_routed` event. A route made before its run's phase record has an empty phase instance id, so the routed-work list prints its key as `<run id>::<finding id>`.
- `findings.py close-route --from` takes the exact key that the routed-work list prints, and it accepts an empty phase instance id. It refuses a key without exactly three parts, and a key with an empty or blank run id or finding id. The refusal message names both forms: `<run id>:<phase instance id>:<finding id>` and `<run id>::<finding id>`.
- The `finding_route_closed` event keeps its shape. No reader of it changed.

**Files:**
- `scripts/direct/findings.py` — `_close_route` checks the `--from` key.
- `scripts/direct/test_findings.py` — one test closes a route from before a phase record through the real routed-work join (`routed_work`); one test shows that nine malformed keys refuse and record no event.

**Ruled out:** a second `--from` form, or a translation from one form to the other — `--from` is always the exact listed key.
