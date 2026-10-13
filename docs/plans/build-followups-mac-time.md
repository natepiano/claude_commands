# Mac time: the build report shows the Mac on its own, and an idle Mac claim ends by itself

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/build:report`'s step tables and summary count natedev alone, and a new Mac section shows the Mac's runs, times and failures. A Mac claim that has had no Mac process for 15 minutes is released, and its holder gets a message saying so and how to claim again.

> **As-built disposition: amend** `docs/as-built/build-targets.md` (the report split and the idle claim) and `docs/as-built/mac-test-offload.md` (the idle claim)

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-13, EDT, the user to build-report-unit directly, after the scheduled build report:

- "do we incorporate mac test times into our summary? i think we should report mac separatley if we do"
- "are we reliably sending tests to the mac? i don't see any running right now"
- "and i do se e a couple of next runs happening here on this machine right now"

Measured 2026-10-13 at 13:08 EDT:

- The step tables leave out the Mac's offloaded runs (`caller = 'verify-mac'`: 68 nextest runs, 7.2 h today). But 8 Mac steps with no recognised caller (6 nextest runs, 28 min) slip into the nextest table and the summary as `unknown (Nates-MacBook-Pro-2023)`. The Mac's own times appear nowhere else, apart from a p50 in Tests by target.
- At 12:46 EDT hana_catalyst-research ran `mac_test.py claim --pid $$ --what 'Phase 11 effects pixel-coordinate repair' --remote natemccoy@mac`, from a `bash -c` that then waits on `read -r release_signal`. Twenty minutes later nothing was running on the Mac, and every eligible test declined as `busy`: 340 of today's 407 usable candidates did. Earlier today, claims named `mac run 46b8c87e5`, `Hana Phase 11 macOS filtered tests at e9c0b8423` and `Hana Phase 11 Metal effects diagnosis` held it the same way.

2026-10-13, the showrunner (natedev), approving both add-ons: "(1) Step tables and summary natedev-only by host, plus a separate Mac section with its runs, times and failures; the user asked for exactly that. (2) A Mac claim with no Mac process for 15 minutes is released automatically, and the holder gets a message saying so and how to claim again; my call, one-line revert if 15 is wrong."

The showrunner's third item, a tree key on Mac runs, was built in build-targets Phase 14 (`docs/as-built/build-targets.md`). Mac nextest rows have carried one since about 02:00 EDT today, and 38 of the 44 keyed Mac runs share a key with a natedev step. The report's "51 without a tree key" are rows from before that change, so they age out of its 24-hour window.

## Design

**The report split.** Every step-based number in `/build:report` counts rows with `host = 'natedev'`, the name `store.host_name()` gives natedev. That covers the Rebuilds table, each kind's table, Memory pressure, the three Summary tables, the lead block's `Failed steps`, and the caller-label host count. `natedev_step_predicate` (`mac_outcome.py`) becomes that host test. Its one other reader keeps working: `mac_report.py` reads Mac steps through `is_mac_step`, never through this predicate. Tables then name callers without `(natedev)`, because only one host is left.

A new `### Mac` section follows Tests by target. It covers every step recorded on a Mac host that day, offloaded or started there directly:

```
### Mac

| Caller | Kind | Runs | Failed | Avg | p95 | Range | Build | Tests | Tests failed |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| verify.sh offload | nextest | <n> | <n> | <time> | <time> | <time> – <time> | <time> | <n> | <n> |
| verify.sh offload | sweep | <n> | <n> | <time> | <time> | <time> – <time> | — | — | — |
| unknown | nextest | <n> | <n> | <time> | <time> | <time> – <time> | <time> | <n> | <n> |
Mac total: <n> steps, <time>; <n> failed.
Source: steps recorded on the Mac; failed follows the build log's failure rule, so a Mac nextest that matched no tests is not a failure.
```

- One row per caller and kind, the busiest first. `verify-mac` reads `verify.sh offload`; every other caller keeps its `caller_label` text.
- `Failed` uses `index.COUNTED_FAILURE`, the rule the index already applies to these rows.
- `Build` is `finished_s`, and `Tests` and `Tests failed` come from nextest's summary. A kind with no such fact shows `—`.
- On a day with no Mac steps the section reads `No steps recorded on the Mac.`

**The idle claim.** A claim is a Mac claim from any caller: offload, `mac_run.sh`, or a session following `/build:mac`. It is released when its Mac has had no Mac process for `claim_idle_minutes` (15) minutes.

- A Mac process is one of the user's processes on the claim's host that matches one of these:
  - the leftover check's two mirror selectors: a command line or working directory under `.local/state/mac-test/mirror/`;
  - an executable named `cargo`, `rustc` or `cargo-nextest`;
  - an executable whose path contains `/target/` (test and app binaries).
  The check never sends a signal.
- The claim's host is its `remote_host`, or `config.host` for a process-bound claim.
- `claim` exit 0 and takeover exit 13 start one claim watcher: a `systemd-run --user` unit named `mac-test-claim-watch-<pid>`, made one at a time by `claim_watch.lock`. The pattern is `start_watcher`'s. The watcher exits when no claim remains.
- Each pass runs every `claim_poll_seconds` (60). It reads the claim under `state.lock`, then makes one ssh activity check under a 10 s limit with no lock held. Then it takes `state.lock` again and acts only if the record is still the same claim (`pid`, `proc_start` and `since` unchanged):
  - **Activity seen:** remove `idle_since` from the record.
  - **None seen, no `idle_since` yet:** write `idle_since` as now.
  - **None seen, `idle_since` at least `claim_idle_minutes` old:** delete `run.json`, start the block watcher when a block is present (as `release` does), then message the holder.
  - **The check failed or could not be read:** change nothing. An unreachable Mac never releases a claim.
- `claim` records the holder's session as `holder_session`: `CLAUDE_CODE_SESSION_ID`, or for a Codex seat its owning session (`build_hold.owning_session`). A claim with no session records none. A takeover keeps the field. `read_run` treats both new fields as optional, so a record written before this change still reads.
- The message goes through `send_message` to `session:<id>`, with key `mac-claim-idle-<pid>-<since>` and no repeat:

  ```
  Your Mac claim "<what>" (since <HH:MM>) was released at <HH:MM>: nothing ran on the Mac for 15 minutes, so tests waiting for it can run. Claim it again before your next Mac command: python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what '<what>' (see /build:mac).
  ```

  A claim with no `holder_session` sends nothing. Either way the release is printed to the watcher's journal.
- `release --pid` for a claim the watcher already removed exits 0, as it does now for a missing claim.
- `mac_test.py status` and `build_targets.py status` add `, idle since <HH:MM>` to a running claim's line when `idle_since` is set.

**Ruled out:**
- Releasing on the leftover check's answer, which kills what it finds.
- Treating the user's own shells under the Mac's clones as activity: they live there all day.
- A message to the showrunner as well as the holder: the holder is the one who can act.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report` (unit `build-report-unit`). Name both in every dispatch.
- **Project started:** 2026-10-13T17:20:00+00:00
- **Stack:** Python 3 standard library only, checked by basedpyright. The activity check's remote text runs under the Mac's `sh` and `zsh -f` (bash 3.2 rules: no associative arrays).
- **Layout:** `scripts/buildlog/` (`report.py`, `mac_outcome.py`, `index.py`, tests), `scripts/mac_test/` (`mac_test.py`, `offload.py`, tests), `scripts/build_targets/` (`build_targets.py`, tests), `config/mac_test.conf`, `commands/build/` (`mac.md`, `report.md`, `targets.md`), `docs/as-built/`.
- **Key files:**
  - `scripts/buildlog/report.py`: `kinds` 383, `caller_label` 395, `daily_waits` 507, `rebuilds_section` 682, `kind_section` 706, `memory_pressure_section` 743, `reported_mac_calls` 901, `tests_by_target_lines` 1000, `SUMMARIES` 1378, `summary` 1381, `lead_block` 1447, `report` 1485. `SCRATCH` and `GROUP_AS_SCRATCH` at 60.
  - `scripts/buildlog/mac_outcome.py`: `MAC_STEP_CALLER` 27, `OFFLOADED_STEP` 28, `is_mac_step` 181, `natedev_step_predicate` 184.
  - `scripts/buildlog/index.py`: `COUNTED_FAILURE` 37. `scripts/buildlog/store.py`: `host_name` 38.
  - `scripts/mac_test/mac_test.py`: `ClaimOwnerRecord` 40, `RemoteJobClaim` 54, `read_run` 423, `run_is_live` 729, `live_run` 737, `send_message` 791, `watcher_is_running` 1202, `start_watcher` 1207, `command_claim` 1445, `command_release` 1511, `command_status` 1756, `command_watch` 1818, `parser` after it. State paths come from `state_paths()` 353.
  - `scripts/mac_test/offload.py`: `leftover_check_script` 991 (its `select_leftovers` function holds the two mirror selectors), `check_leftovers` 1031.
  - `scripts/build_targets/build_targets.py`: `command_status` 107.
  - `scripts/build_hold/build_hold.py`: `owning_session` 481.
- **Test lanes:** `scripts/buildlog/` → `test_report.py`, `test_mac_outcome.py`, `test_mac_report.py` · `scripts/mac_test/` → `test_mac_test.py`, `test_offload.py` · `scripts/build_targets/` → `test_build_targets.py`.
- **Build:** none; the code is interpreted.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'` for each directory a phase touches.
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - The Mac claim stays one job at a time. The watcher only removes a claim that is idle and unchanged since it read it.
  - No test contacts the Mac or starts a real `systemd-run` unit.
  - Installed scripts refuse `MAC_TEST_STATE_DIR`; tests set it for a checkout's scripts.
  - Script arguments and exit codes stay as they are; `claim` gains no new required argument.

## Phases

### Phase 1 — `/build:report` counts natedev alone, and the Mac gets its own section  · status: done

#### As-built

- `natedev_step_predicate(step="steps")` in `scripts/buildlog/mac_outcome.py` returns `{step}.host = 'natedev'`. Every step reader in `report.py` takes it: the kind tables, Rebuilds, lead counts, waits, test routing, caller-host counts, the Summary, and Memory pressure's waits, longest wait, stalls with their concurrent counts, and the unsliced-step, sccache-service and memory-kill lines (through `memory.instrument_lines`, in its unqualified form).
- `mac_section(connection, day)` follows Tests by target as `### Mac`: Caller | Kind | Runs | Failed | Avg | p95 | Range | Build | Tests | Tests failed, one row per caller and kind over every non-natedev host, busiest first. `verify-mac` shows as "verify.sh offload"; other callers use `caller_label`. Failed uses `index.COUNTED_FAILURE`, so a Mac nextest that matched no tests is not a failure. Build is the mean `finished_s`; missing test facts show as `—`. A `Mac total: <steps> steps, <time>; <n> failed.` line and a Source line close it. A day with no Mac steps prints "No steps recorded on the Mac."
- `commands/build/report.md` says the step tables, Rebuilds, Memory pressure and Summary count natedev's steps and the Mac section holds the Mac's.

**Files:**
- `scripts/buildlog/mac_outcome.py` — `natedev_step_predicate`, the host rule for local step readers
- `scripts/buildlog/report.py` — natedev filters on every step reader; `mac_section`
- `scripts/buildlog/test_report.py`, `scripts/buildlog/test_mac_outcome.py` — mixed-host fixtures for both
- `commands/build/report.md` — which host each set of tables covers

**Gotchas:** the log carries two Mac host names (`Mac` and `Nates-MacBook-Pro-2023`), so Mac selection is `host <> 'natedev'`, never one Mac name. The `scripts/buildlog/buildlog` wrapper runs the installed copy; worktree code is checked with `~/.claude/scripts/lib/py scripts/buildlog/cli.py` and `BUILDLOG_DIR` on a scratch copy of the log.

### Phase 2 — A Mac claim with no Mac process for 15 minutes is released, and its holder is told  · status: done

#### Work Order

**Goal:** A Mac claim whose host has had no Mac process for `claim_idle_minutes` (15) minutes is released by a claim watcher, and the holder's session gets one message saying so and how to claim again.

**Spec:**
- `config/mac_test.conf` gains `claim_idle_minutes=15` and `claim_poll_seconds=60`, each with a comment, read into `MacTestConfig`.
- `mac_test.py`:
  - `ClaimOwnerRecord` gains optional `holder_session` and `idle_since`, and `read_run` decodes both.
  - `command_claim` records `holder_session` and starts the claim watcher after exit 0 and after exit 13.
  - A new `watch-claim` subcommand runs the pass loop under `claim_watch.lock`.
  - `start_claim_watcher(paths)` follows `start_watcher`, with unit `mac-test-claim-watch-<pid>`.
  - `check_mac_activity(config, host) -> MacActive(count) | MacIdle | ActivityCheckFailed(reason)` makes the one ssh call, using a remote text that prints `active=<n>` then `rc=0`. Its mirror selectors share `offload.py`'s text and do not copy it: factor the selector function out of `leftover_check_script` so both texts use it.
  - `command_status` adds the `idle since` clause.
- `build_targets.py`: the Mac's running line gets the same clause.
- Tests:
  - `test_mac_test.py`:
    - the four pass outcomes;
    - a pass that finds a different claim, or no claim, changes nothing;
    - the release message's target, key and text, and no message without `holder_session`;
    - the holder session from `CLAUDE_CODE_SESSION_ID` and from a seat's owning session;
    - a takeover keeps `holder_session`;
    - an old record without the new fields still reads;
    - `claim` starts the claim watcher on exits 0 and 13 and on no other exit;
    - `release --pid` after a watcher release exits 0.
  - `test_offload.py`: the remote activity text runs under `sh` and `zsh -f` with stand-in `pgrep`, `lsof` and `ps`, and prints `active=0` and a counted case. The leftover text's existing cases still pass.
  - `test_build_targets.py`: the `idle since` clause.
- `commands/build/mac.md`: after the claim instruction, one sentence saying a claim with nothing running on the Mac for 15 minutes is released and the holder is told, so claim right before the remote work and claim again after a release.
- Constraints from prior phases: a wrapper under `scripts/<tool>/` execs the installed `~/.claude/scripts` copy, so the live check runs this worktree's file directly (`~/.claude/scripts/lib/py <worktree>/scripts/mac_test/mac_test.py`), with `MAC_TEST_STATE_DIR` and `MAC_TEST_CONFIG` pointing at scratch copies; `override_guard` refuses those overrides on the installed copy. `start_claim_watcher` runs `Path(__file__)` and forwards the `MAC_TEST_*` environment, as `start_watcher` does, so the watcher it starts is the same file with the same scratch state.

**Files:**
- `config/mac_test.conf`
- `scripts/mac_test/mac_test.py`
- `scripts/mac_test/offload.py`
- `scripts/mac_test/test_mac_test.py`
- `scripts/mac_test/test_offload.py`
- `scripts/build_targets/build_targets.py`
- `scripts/build_targets/test_build_targets.py`
- `commands/build/mac.md`

**Seats:** 1 writer + 1 tester.

**Acceptance gate:** both lanes pass. A live check on natedev against the real Mac, with no Mac work running, shows the release and the message. In it, a scratch session claims with `--remote mac` and a short-lived `claim_idle_minutes` set in a scratch config copy. The check never touches `~/.local/state/mac-test` while a real claim is present.

### Phase 3 — Each test run sent to the Mac deletes its build output when it finishes  · status: todo

#### Work Order

**Goal:** after each offloaded test run, the Mac mirror's target dir for that repository is deleted, so offloaded runs stop filling the Mac's disk. Nothing else on the Mac is touched, `/tmp` included. Source: the user, 2026-10-13 ("it needs to be cleaned up as we go", "we should only be cleaning up nextest target dirs as we go along - you should leave the rest of /tmp alone of course"); the showrunner chose this form over a size-capped trim (simple ranks above fast).

**Spec:**
- `offload.py` `remote_test_command`: after `echo mac_exit=$?`, the remote text deletes `"$HOME"/.local/state/mac-test/mirror/<repo>/target` (the path built from `remote_mirror`, quoted) whatever the run's exit code was, and prints `mac_cleanup=<rc>` on its own line. Before writing it, confirm that neither the mirror's `.cargo/config.toml` nor the remote command sets a different target dir. If one does, delete that dir instead and record it in the summary.
- A failed delete never changes the run's result. The offload reports it as one line, `Mac build output not removed: <rc>`, and continues.
- The parser that reads `mac_exit=` also reads `mac_cleanup=`, as a named outcome type, not a bare optional.
- Tests (`test_offload.py`): run the remote text under `sh` and `zsh -f` with a stand-in `lint`. The target dir is gone after a passing run and after a failing run. A sibling path in the mirror and a file in a stand-in `/tmp` survive. `mac_exit` is unchanged. A failed delete yields the report line and the run's own result.

**Files:**
- `scripts/mac_test/offload.py`
- `scripts/mac_test/test_offload.py`

**Seats:** 1 writer + 1 tester.

**Acceptance gate:** the mac_test lane passes. A live check runs only while no real Mac claim is held: one hana nextest offload from natedev leaves no `target` under the Mac's hana mirror afterwards. The phase report gives that run's Mac wall time, built from nothing, beside the last warm hana Mac runs and natedev's time for the same tests, all from buildlog. If the cold Mac run is slower than natedev, tell the showrunner before the checkpoint notice.
