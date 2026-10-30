# sdlc devtools: faster repo tests with less CPU

Owner: sdlc devtools-unit. Asked by the user through the sdlc showrunner, 2026-10-30: "it can also be where we look at making repo tests go faster and take less cpu".

The measured inventory (slowest modules, CPU per module, repeated setup, copies of per-file test config) and its phases come after Phase 1. Known inputs for it:

- `scripts/agents/test_codex_mesh.py`: killed at the 1800 s module limit under load (gate_merge77.log), about 1251 s unloaded, 741 s in a later gate; it grows with each mesh test. Check first if herdr's plan retires the mesh soon.

## Phase 1 — The merge gate finds every test module itself

**Cause.** `TEST_SUITES` at `scripts/production/merge_gate.py:63` is a hand list. A folder with tests runs in a gate only when someone adds it to the list, and `scripts/production/merge_gate_test_support.py` `EXPECTED_SUITES` is a second copy of it. Eleven folders were never added, so 22 modules never ran in any gate: `review_links`, `exclude`, `hana_shot`, `nix_env`, `screen_record`, `settings`, `shot_report`, `validate_and_push`, `worktree_delete`, and the nested `scripts/berth/tests/` and `scripts/prioritize/tests/`.

**Once-run, 2026-10-30, the gate's way (cwd = repo, `unittest discover -s <folder> -p <file>`):** 22 of 24 modules green, including `nightly_review/test_candidates.py` and `style/test_add_style.py`, which hierarchies asked about. Two red:

- `berth/tests/test_hook_rendering.py`: `ModuleNotFoundError: No module named 'berth'`. This module checks the installed front end (`~/.cargo/bin/cargo-berth` and the hooks under `~/.claude/scripts`), not the checkout. Its setUpClass raises when nothing is installed. A merge gate tests the checkout, so this module is not a gate test.
- `shot_report/test_transcripts.py`: 8 failures, but only when TMPDIR is inside a Claude scratchpad path. The code under test decodes scratchpad paths and finds the live `~/.claude` repository. The gate's TMPDIR is under `/dev/shm`, and there it is green (90 tests). Sent to natedev's screenshot-unit as a low item.

**Change.**

1. `merge_gate.py`: delete `TEST_SUITES`. One function finds the modules: every `scripts/**/test_*.py` outside `__pycache__`, sorted. The suite of a module is its first folder under `scripts/`. Everything that used the list uses the found names: the passed-tree ledger `suites` field (written and compared as the sorted found names), and the per-suite report. The refusal "suites have no test modules" goes, because a suite now exists only through its modules. The refusal "the test list is empty" stays. A nested module runs as all modules do now: `discover -s <its folder> -p <its file>`, cwd = repo.
2. `merge_gate_test_support.py`: `EXPECTED_SUITES` and the `merge_gate.TEST_SUITES == EXPECTED_SUITES` check go. The fixture keeps the names of the folders it writes as fixture data. `test_merge_gate_ledger.py:323` patches `TEST_SUITES`; it uses the fixture's folders instead. New tests: a folder that no code names runs; a module in a nested `tests/` folder runs under its top folder's suite; a folder without `test_*.py` is no suite; a changed suite set does not reuse or certify an old ledger record.
3. Rename `scripts/berth/tests/test_hook_rendering.py` to `scripts/berth/tests/check_installed_hooks.py`. Add one line to `scripts/berth/install/README.md` that tells how to run it after an install (`cd ~/.claude/scripts && python3 -m unittest berth.tests.check_installed_hooks`).
4. Docs that state the list: `docs/as-built/merge-gate-and-test-speed.md` (:9, :52, :61, :171), `docs/as-built/long-lived-workers.md:452`, `docs/as-built/style-showrunner.md:43`, `docs/as-built/bevy-upgrade.md:441`.

**Cost.** The ledger key changes one time, so the first gate after this change runs in full. The new modules add about 120 module-seconds (`hana_shot` 58 s, `screen_record` 36 s, the rest under 3 s each). A new folder with tests later also changes the key and forces one full run; that is correct, because the old record did not cover it.

**Check.** Run the checkout's own gate in full: `merge_gate.py --tests-only --full --checkout <phase tree>`, and confirm that the report names all 40 suites (29 listed today plus 11 new). Type-check `scripts/production` with basedpyright: 0 errors, 0 warnings.

**Agreed.** natedev, 2026-10-30: "take it"; no open natedev branch touches `merge_gate.py`.
