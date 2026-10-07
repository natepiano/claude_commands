# Mac opportunistic nextest

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `verify.sh test` tries the Mac first when it is free, and a showrunner can block the Mac and is told when it is free.

> **As-built disposition: create**

> **Production: build-followups** — unit `build-report-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` config repo: scripts and commands every Claude Code and Codex session on natedev and the Mac runs. This plan sends `verify.sh test` runs to the Mac when it is idle.
- **Stack:** Python 3 standard library only (`unittest`, `argparse`, `fcntl`, `subprocess`, `shlex`, `tomllib`), checked by basedpyright; bash (`scripts/delegate/verify.sh`, `scripts/lint/invoke.sh`); zsh (`scripts/production/mac_run.sh`); `ssh` and `rsync` to host `mac` (Tailscale SSH).
- **Layout:** `scripts/mac_test/` (new: state CLI, offload runner, audit, tests) · `scripts/delegate/` (`verify.sh` and its `test_verify_*.py`) · `scripts/production/mac_run.sh` · `scripts/buildlog/` (`record.py`, `index.py`, `report.py`, tests) · `config/` · `commands/`.
- **Key files:**
  - `scripts/delegate/verify.sh` — the one command workers run. `--no-cache` is stripped at 493-502; first `cache_lookup` 700-702; cargo token acquire 721-735; `note_event` 561-580; the `test)` arm 904-966; usage text is the header comment 70-111.
  - `scripts/delegate/test_verify_token_wait.py` — fixture to copy: temp `HOME` with `.claude` symlinked to the repo root, stand-in `cargo` and `git` on `PATH` (lines 34-147), `records()` reads call records (175-178).
  - `scripts/delegate/test_verify_untested_examples.py` — asserts the nextest argv `verify.sh test` builds; it must keep passing unchanged.
  - `scripts/lint/lint` — public entry on the Mac: `lint nextest <args>` (104-105) sources `invoke.sh` and runs `cargo nextest run <args>` as a recorded step.
  - `scripts/lint/sweep.py` — `config_values(path)` (1174-1190) reads `key=value` conf files; `repo_name()` (1193-1222) names a repository by the directory holding its git common dir.
  - `scripts/lint/memory_admit.py` — 211-216: a record is live only while `/proc/<pid>/stat` start time matches.
  - `scripts/build_hold/build_hold.py` — 635-647: resolve a session's socket with `scripts/message/sessions.py socket session:<id>`, then `scripts/message/send.py --to uds:<socket>`; 718-723: send exit 0 and 1 both count as delivered.
  - `scripts/production/mac_run.sh` — the showrunner's Mac build; reach probe at line 45, `on_mac()` 36-43.
  - `scripts/production/test_merge_checkpoint.py` — 44-58, 147: one stand-in script installed as `ssh` on `PATH`, logging argv and printing the remote status as text.
  - `scripts/buildlog/record.py` — `call` fields from env (320-340; `token_wait_s` at 333). `scripts/buildlog/index.py` — `calls` columns 107-141, old-record default 381, `SCHEMA_VERSION` 33. `scripts/buildlog/report.py` — `report(connection, day)` 951-975, `known_crate_steps` 378-384, `rebuilds_section` 473-494.
  - `scripts/buildlog/test_report.py` — fixture 50-57; record builders in `scripts/buildlog/test_index.py` 41-193.
  - `commands/build_hold.md` — model for a short command doc. `config/README.md` — one `## <file name>` heading and a paragraph per config file. `pyrightconfig.json` — `executionEnvironments` with `extraPaths` per script directory.
- **Test lanes:** `scripts/mac_test/` → `scripts/mac_test/test_*.py` (new) · `scripts/delegate/` → `scripts/delegate/test_verify_*.py` · `scripts/buildlog/` → `scripts/buildlog/test_*.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s <directory> -p '<pattern>'` from the repo root, with the directory and pattern each phase names.
- **Lint:** `basedpyright <directory>`. A pass is its last line `0 errors, 0 warnings, 0 notes`; it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has. Shell files: `bash -n <file>` or `zsh -n <file>`.
- **Invariants:**
  - Any doubt means local. Every Mac-side trouble except the test command's own exit status falls back to today's local run, unchanged. A worker never waits on the Mac longer than the probe timeout.
  - Pass records are written only by the natedev run. A Mac pass never stands as a gate.
  - Never trust ssh's exit status: Tailscale SSH returns 0 whatever the remote command did. Status is a printed `mac_exit=<n>` or `rc=<n>` line; ssh's own 255 or a timeout means unreachable.
  - Tests run nothing on the Mac: stand-in `ssh` and `rsync` on `PATH`, state under `MAC_TEST_STATE_DIR`, config under `MAC_TEST_CONFIG`.
  - On the Mac the feature writes only under `~/.local/state/mac-test/`. Never touch `~/rust/hana`, `~/rust/hana_catalyst_mac`, `~/rust/bevy_hana` or `~/rust/hana_open_freeze_1791356904`.
  - Only step records are written on the Mac. Call records are written on natedev only: `calls.id` is replaced on a second write.
  - Python: basedpyright zero errors and zero warnings, no file-level ignores, no `Any` (a `TypedDict` for each JSON shape).
  - `settings.json` is not edited.
  - `config/mac_test.conf` ships with `offload=off`; only the last phase turns it on.
  - Mac facts, read 2026-10-07: `hostname -s` is `Mac`, 12 cores, 64 GiB memory, 187 GiB free, bash 5.3 and `~/.cargo/bin/cargo` on the non-login ssh `PATH`, `/usr/bin/rsync` is Apple's (plain options only).

## Phases

### Phase 1 — The Mac's lock, block and "Mac is free" message  · status: todo

#### Work Order

**Goal:** One command on natedev says whether the Mac may take a test, lets a showrunner block it, and messages the blocker when a running test ends.

**Spec:**
New CLI `scripts/mac_test/mac_test.py` (`argparse`). State directory: `MAC_TEST_STATE_DIR`, default `~/.local/state/mac-test/`. Every command takes `fcntl.flock` on `state.lock` there for its whole read-modify-write; files are written to a temp name then `os.replace`.

- `run.json`: `{"pid": int, "proc_start": str, "what": str, "worktree": str, "since": "<ISO-8601 UTC>"}`. A run is live only while `/proc/<pid>/stat` field 22 equals `proc_start` (`scripts/lint/memory_admit.py:211-216`).
- `block.json`: `{"holder": str, "session": str | null, "for": str, "since": str, "state": "pending" | "active"}`. One block at a time.
- `settle()` runs first in every command, under the lock: remove a `run.json` whose process is gone; then, if the block is `pending` and no run is live, set it `active` and send the message below.

Commands, with exit codes:
- `claim --pid PID --what TEXT [--worktree PATH] [--wait SECONDS]` — a block of either state: print `blocked by <holder>: <for>`, exit 10. A live run: poll every 2 s up to `--wait` (default 0), then print `busy: <what> since <HH:MM>`, exit 11. Otherwise write `run.json` for PID, print `claimed`, exit 0.
- `release --pid PID` — remove `run.json` when its pid is PID, then settle. Exit 0 either way.
- `block --holder NAME --for TEXT` — `session` is `CLAUDE_CODE_SESSION_ID` when set. No live run: state `active`, print `Mac blocked for <holder>: nothing is running there, it is free now.` A live run: state `pending`, print `Block pending for <holder>: <what> is running on the Mac. Nothing new starts there, and you get a message when it ends.` A block by another holder already there: print `already blocked by <holder>`, exit 1. The same holder again replaces the reason.
- `unblock --holder NAME` — remove the block when NAME holds it and print `Mac unblocked.`; another holder's block: name the holder, exit 1; no block: print `no block`, exit 0.
- `status` — one line for the run (`free`, or `running: <what> (<worktree>) since <HH:MM zone>`) and one for the block (`no block`, `blocked by <holder> since <HH:MM zone>: <for>`, or `block pending for <holder>: <for>`). Times in the local zone.

The message, sent once when a pending block turns active: first line `Message from mac-test: the Mac is free. <what> ended at <HH:MM zone>; your block (<for>) is active, and nothing is built or tested there until you run unblock.` Delivery follows `scripts/build_hold/build_hold.py:635-647`: with a recorded session, run `scripts/message/sessions.py socket session:<id>` and send with `scripts/message/send.py --to uds:<socket> --from mac-test --summary 'Mac is free' --key mac-free-<since> --text <text>`; with no session, or when `sessions.py` exits 1, send `--to <holder>` (it queues). send.py exit 0 or 1 is delivered; exit 3 prints one warning line on stderr and the block still turns active. Both scripts are found beside this directory (`Path(__file__).resolve().parent.parent / "message"`); `MAC_TEST_SEND` and `MAC_TEST_SESSIONS` override the two paths for tests.

`commands/mac_test.md`, modelled on `commands/build_hold.md`: `$ARGUMENTS` is `block <why>`, `unblock` or `status`; each runs `python3 ~/.claude/scripts/mac_test/mac_test.py …` with `--holder <your session name>`. It says in plain words: a block stops new tests and builds on the Mac, a test already running finishes, and the "Mac is free" message arrives when it does.

**Files:**
- `scripts/mac_test/mac_test.py` — new: the state CLI.
- `scripts/mac_test/test_mac_test.py` — new: its tests.
- `commands/mac_test.md` — new: the command doc.

**Seats:** `1 writer + 1 tester` — the CLI and its doc are one writer's; the tests come from this Spec.
- `impl` — `scripts/mac_test/mac_test.py`, `commands/mac_test.md`
- `test` — `scripts/mac_test/test_mac_test.py`: every command's output and exit code, each state change, and the message, using stand-in `send` and `sessions` scripts that log their argv and a child process as the "run".

**Constraints from prior phases:** None.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`, and `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Tests prove: a claim under a block exits 10; a second claim exits 11 and `--wait` gets it once the first releases; a block placed during a run is pending, and the release sends exactly one message and turns it active; a run whose process was killed is settled by the next `status`, which sends the message; another holder cannot unblock; with `sessions.py` answering 1 the message goes to the holder's name.

### Phase 2 — The offload runner: probe, copy the tree, run on the Mac  · status: todo

#### Work Order

**Goal:** One command takes a nextest argv and either runs it on the Mac against a copy of the worktree, streaming the output back, or says in one word why the run stays local.

**Spec:**
New `scripts/mac_test/offload.py`:
`offload.py run --repo-root DIR --package PKG --call-id ID [--filter-run] [--result FILE] -- <words after "cargo nextest run">`

Exit codes: `0` passed on the Mac; `75` not run there (declined or lost; the run must happen locally); any other value is the Mac run's own exit status. With `--result`, write three lines: `mac=<passed|failed|lost|declined>`, `reason=<word or empty>`, `seconds=<float>`. A decline prints one line `mac_test: staying on natedev (<reason>)`.

Config: `MAC_TEST_CONFIG`, default `~/.claude/config/mac_test.conf`, read with `config_values()` imported from `scripts/lint/sweep.py` (the import form of `scripts/buildlog/disk.py:15-16`). New file `config/mac_test.conf`, each key commented in plain words:
```
offload=off
host=mac
connect_timeout_s=6
probe_timeout_s=15
unreachable_backoff_s=300
free_floor_gib=60
max_load=6
mac_budget_gib=24
mac_budget_gib.hana=64
linux_only.hana=hana_video,hana_clerestory,hana_prosody
mac_skip.hana=
```
A repository is offloadable only when a `linux_only.<repo>` key exists, empty or not. `<repo>` is the name of the directory holding the git common dir (`git -C <root> rev-parse --git-common-dir`, as `repo_name()` in `sweep.py`).

Steps, in order; each failure is a decline with the reason shown:
1. `offload` is not `on` → `off`. No `linux_only.<repo>` key → `repo`. PKG in that list → `linux_only`.
2. `unreachable_until` in the state directory is in the future → `backoff`.
3. `mac_test.py claim --pid <own pid> --what "nextest <PKG>" --worktree <root>`: exit 10 → `blocked`, 11 → `busy`. From here on, always run `mac_test.py release --pid <own pid>` before exiting, on signals too.
4. Probe, one ssh call: `ssh -o BatchMode=yes -o ConnectTimeout=<connect_timeout_s> <host> '<script>'` under `probe_timeout_s`. The script makes the mirror directory `.local/state/mac-test/mirror/<repo>` and prints `procs=<count of cargo, rustc and cargo-nextest processes of any user>`, `load=<1-minute load>`, `power=<ac|battery>`, `free_gib=<free GiB on the home volume>`, `host=<hostname -s>`, `rc=0`, using `pgrep -x`, `sysctl -n vm.loadavg`, `pmset -g batt` and `df -g "$HOME"`. ssh exit 255, a timeout or no `rc=0` line → write `unreachable_until = now + unreachable_backoff_s`, reason `unreachable`. `procs` above 0 or `load` above `max_load` → `mac_busy`. `power=battery` → `battery`. `free_gib` below `free_floor_gib` → `disk`.
5. Copy: write an exclude file holding `/.git`, `/target/` and one anchored line per path from `git -C <root> ls-files --others --ignored --exclude-standard --directory`. Then `rsync -rlpc --delete --exclude-from=<file> -e "ssh -o BatchMode=yes -o ConnectTimeout=<n>" <root>/ <host>:.local/state/mac-test/mirror/<repo>/`. No `-t` and no `-a`: a file whose content is unchanged keeps its Mac modification time, so cargo rebuilds only what changed, whichever worktree the copy came from. A nonzero rsync status → `copy`.
6. Run: print `mac_test: running on the Mac (macOS, host <host>); tree copied in <s> s`. Then one ssh call whose remote command is, built with `shlex.join` and `shlex.quote`:
   `cd <mirror> && PATH="$HOME/.cargo/bin:$PATH" BUILDLOG_CALLER=verify BUILDLOG_CALL_ID=<ID> BUILDLOG_SYNC=1 LINT_SWEEP_BUDGET_GIB=<mac_budget_gib.<repo>, else mac_budget_gib> ~/.claude/scripts/lint/lint nextest <words>; echo mac_exit=$?`
   When `mac_skip.<repo>` is not empty, the value after `-E` becomes `(<given>) & not (<mac_skip>)`. Stream the remote stdout and stderr line by line as they arrive; hold back the last `mac_exit=<n>` line. No such line → `lost`, exit 75. `mac_exit=0` → exit 0. Otherwise print `mac_test: this ran on the Mac (macOS). A failure that looks unrelated to your change may be a macOS difference; rerun with --local to run it on natedev.` and exit `<n>`.
7. On SIGTERM or SIGINT: stop the ssh child, try once for at most 10 s to end what it started (`ssh <host> "pkill -f <mirror>"`), release, and exit 128 plus the signal number.

`config/README.md` gains a `## mac_test.conf` entry. `pyrightconfig.json` gains `{"root": "scripts/mac_test", "extraPaths": ["scripts/mac_test", "scripts/lint"]}`.

**Files:**
- `scripts/mac_test/offload.py` — new: the runner.
- `scripts/mac_test/test_offload.py` — new: its tests.
- `config/mac_test.conf` — new.
- `config/README.md` — the new entry.
- `pyrightconfig.json` — the new environment entry.

**Seats:** `1 writer + 1 tester` — the runner and its config are one writer's; the tests come from this Spec.
- `impl` — `scripts/mac_test/offload.py`, `config/mac_test.conf`, `config/README.md`; hub: `pyrightconfig.json` (the tester's imports resolve through it)
- `test` — `scripts/mac_test/test_offload.py`: one stand-in script installed as `ssh` and `rsync` on `PATH` (the form in `scripts/production/test_merge_checkpoint.py:44-58`), logging argv, with knobs for each probe line, `mac_exit=<n>`, ssh's own 255, a slow answer and a missing last line.

**Constraints from prior phases:** `scripts/mac_test/mac_test.py` exists with `claim` (0 claimed, 10 blocked, 11 busy) and `release`, state under `MAC_TEST_STATE_DIR`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Tests prove: each decline reason, with exit 75 and the result file; a second call inside the back-off makes no ssh call; the rsync argv and the exclude file; the remote command carries the call id, the budget and the exact nextest words, with a filter such as `package(hana) & (test(a) | test(b))` surviving quoting; `mac_skip` wraps the filter; output order is kept and the `mac_exit` line is not shown; exit 0, a failing status and a lost connection; the claim is released on every path, a signal included.

### Phase 3 — `verify.sh test` tries the Mac first  · status: todo

#### Work Order

**Goal:** A worker's `verify.sh test <package>` fails fast on the Mac when the Mac is free, a pass there is confirmed on natedev, and the showrunner's Mac build honours the same lock and block.

**Spec:**
`scripts/delegate/verify.sh`:
- `--local` is stripped beside `--no-cache` (493-502) into `LOCAL_ONLY=1`, so it is in neither the tree key nor the call record's words. Add its usage line to the header comment (70-111).
- For `test`, the nextest words are composed before the cargo token is taken. Move the `test)` arm's parsing and target selection (905-966) into a function that fills `NEXTEST_ARGS` (the words after `cargo nextest run`) and runs after the first `cache_lookup` (700-702); the arm then only calls `run_nextest "${NEXTEST_ARGS[@]}"`. The local argv stays byte-identical to today's.
- Between that and the token acquire (721-735), when the first lookup missed and `LOCAL_ONLY` is 0, call the runner: `"$PY" "$MAC_RUNNER" run --repo-root "$(git rev-parse --show-toplevel)" --package "$PKG" --call-id "$BUILDLOG_CALL_ID" --result <temp file> [--filter-run] -- "${NEXTEST_ARGS[@]}"`, its output going straight to the caller. `MAC_RUNNER` is `${VERIFY_MAC_RUNNER:-$HOME/.claude/scripts/mac_test/offload.py}`; a missing file skips the step.
  - Exit 75: continue into the local path, unchanged.
  - Exit 0 on a `--filter` run: print `verify.sh: PASS on the Mac (macOS). A filtered run is feedback, so nothing ran on natedev.`, write the call record, exit 0. No pass record.
  - Exit 0 otherwise: print `verify.sh: passed on the Mac (macOS); now confirming on natedev, which is the gate.` and continue into the local path.
  - Any other status: write the call record with that status and exit with it. No token, no local run, no pass or fail record.
- `note_event` (561-580) passes `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON` and `BUILDLOG_MAC_S` to `record.py call` beside `BUILDLOG_TOKEN_WAIT_S` (577), read from the result file. `--local` records `declined` with reason `local_flag`. `record.py` ignores them until Phase 5.
- An interrupt while the runner is active reaches the runner, which releases its claim.
- No other verb calls the runner; `final` stays on natedev.

`scripts/production/mac_run.sh`: after the reach probe (line 45), `python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what "mac run ${sha[1,9]}" --wait 600`. Exit 10 or 11: print the line it gave and exit 3, which the showrunner's rule already reads as "skip". Release on every exit path. Update the header's exit list.

`commands/showrunner/produce.md`, the Mac run rule (503-509): add that exit 3 also means the Mac is blocked or busy with a test. `commands/unit/delegate.md`, the `<VerificationContract/>` table: add the row `a Mac failure that looks unrelated to the change, run on natedev` → `bash ~/.claude/scripts/delegate/verify.sh test <package> --local`.

**Files:**
- `scripts/delegate/verify.sh` — `--local`, the composed argv, the Mac step, the call record fields.
- `scripts/delegate/test_verify_mac_offload.py` — new.
- `scripts/production/mac_run.sh` — claim and release.
- `commands/showrunner/produce.md` — one clause in the Mac run rule.
- `commands/unit/delegate.md` — one table row.

**Seats:** `1 writer + 1 tester` — one writer holds the scripts and docs; the tests come from this Spec.
- `impl` — `scripts/delegate/verify.sh`, `scripts/production/mac_run.sh`, `commands/showrunner/produce.md`, `commands/unit/delegate.md`
- `test` — `scripts/delegate/test_verify_mac_offload.py`: the fixture of `test_verify_token_wait.py` (its stand-in `cargo` extended to answer `nextest --version` and `nextest run`), with `VERIFY_MAC_RUNNER` pointing at a stand-in runner that logs its argv, writes the result file and exits as told.

**Constraints from prior phases:** `scripts/mac_test/offload.py run` exits 0 (Mac pass), 75 (run locally) or the Mac run's status, and writes `mac=`, `reason=`, `seconds=` lines to `--result`. `scripts/mac_test/mac_test.py claim --wait` exits 0, 10 or 11. `config/mac_test.conf` ships `offload=off`, so nothing reaches the Mac yet.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` ends `OK` with `test_verify_untested_examples.py` unchanged; `basedpyright scripts/delegate` ends `0 errors, 0 warnings, 0 notes`; `bash -n scripts/delegate/verify.sh` and `zsh -n scripts/production/mac_run.sh` pass. Tests prove: the runner gets the same words the local run would; exit 75 runs locally with the token as before; a Mac failure exits with that status, runs no local cargo and writes no pass record; a Mac pass on a full run then runs locally and only that run writes the pass record; a Mac pass on a `--filter` run ends there; `--local` never calls the runner and a recorded pass answers both forms; `check`, `lint` and `final` never call it; the call record carries the three fields.

### Phase 4 — Which packages are pointless on the Mac  · status: todo

#### Work Order

**Goal:** One command lists, for any Rust workspace, the packages whose tests depend on the operating system, so the Linux-only list can be checked.

**Spec:**
New `scripts/mac_test/audit.py <repo dir> [--check]`. It reads the root `Cargo.toml` with `tomllib`, expands `[workspace] members` globs and drops `exclude` entries; it runs no cargo. For each member:
- **gated sites:** lines in `src/`, `tests/` and `build.rs` matching `target_os`, `cfg(unix)`, `cfg(windows)`, `target_family`, counted for source and tests apart;
- **target dependencies:** the keys of the member's `[target.'cfg(…)'.dependencies]` and `dev-dependencies` tables;
- **listed:** whether `linux_only.<repo>` in `MAC_TEST_CONFIG` names it;
- **suggestion:** `keep on natedev` when a target table names linux or macos, else `can go to the Mac`.

Output is a markdown table: `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`, largest counts first, then one line naming each package whose suggestion and listing disagree. `--check` exits 1 when a `keep on natedev` package is not listed. `commands/mac_test.md` gains `audit <repo dir>` with one line on reading the table: gating is at compile time, so a gated test does not fail on the Mac, it is simply not built there.

**Files:**
- `scripts/mac_test/audit.py` — new.
- `scripts/mac_test/test_audit.py` — new.
- `commands/mac_test.md` — the `audit` argument.

**Seats:** `1 writer + 1 tester` — one small command; the tests come from this Spec.
- `impl` — `scripts/mac_test/audit.py`, `commands/mac_test.md`
- `test` — `scripts/mac_test/test_audit.py`: a temp workspace of three members (one with a linux target table, one with gated tests only, one plain), member globs and `exclude`, the table, the disagreement line and `--check`.

**Constraints from prior phases:** `config/mac_test.conf` holds `linux_only.<repo>=<comma list>`; read it with `config_values()` from `scripts/lint/sweep.py`, as `offload.py` does. `commands/mac_test.md` exists with `block`, `unblock`, `status`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`.

### Phase 5 — The build log and the report show what the Mac took  · status: todo

#### Work Order

**Goal:** The build report says how many test runs the Mac took, how they went, why others stayed local, and how much natedev time and waiting that removed.

**Spec:**
- `scripts/buildlog/record.py call`: read `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON`, `BUILDLOG_MAC_S` into fields `mac`, `mac_reason`, `mac_s`, as `token_wait_s` is read (320-340).
- `scripts/buildlog/index.py`: `calls` gains `mac TEXT`, `mac_reason TEXT`, `mac_s REAL` (107-141); records without them read as null (the default form at 381); `SCHEMA_VERSION` 9 → 10 (33). `scripts/buildlog/test_index.py:463-469` asserts the version.
- An **offloaded step** is a step whose `call_id` joins a call recorded on another host. `known_crate_steps` (378-384) and the waiting, kind, test-builds and tests-per-edit sections leave offloaded steps out: they describe natedev.
- `scripts/buildlog/report.py`: new `mac_tests_section(connection, day)`, after `test_builds_section` in `report()` (951-975), omitted when no call that day has `mac` set. Heading `Tests on the Mac`. One row per package: `Package | Tried | Failed on the Mac | Passed there | Mac p50 | natedev p50 | Stayed local`, where `Tried` counts calls with `mac` of `passed`, `failed` or `lost`, `Mac p50` is the median `mac_s` of `passed` and `failed`, `natedev p50` is the median `wall_s` of that day's natedev test calls for the package, and `Stayed local` lists each decline reason with its count in plain words (`blocked 3, Mac busy 2`). One `Source:` line follows the table. Then two lines:
  - `natedev runs avoided: <N>` — calls that failed on the Mac — `about <X> min of build and test time and <Y> min of waiting, estimated from that day's natedev runs of the same packages` (N times the package's median `wall_s`, and N times its median `wait_s` plus `token_wait_s`).
  - `Failed on the Mac, then passed on natedev with --local: <N> (<packages>)` — a `failed` call followed within 30 minutes, same worktree and package, by a call with reason `local_flag` and status 0. Omitted at zero.

**Files:**
- `scripts/buildlog/record.py` — the three call fields.
- `scripts/buildlog/index.py` — columns, defaults, schema version.
- `scripts/buildlog/report.py` — the section and the offloaded-step filter.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py` — tests.

**Seats:** `1 writer + 1 tester` — record, index and report are one chain; the tests come from this Spec.
- `impl` — `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py`
- `test` — the three test files: the fields round-trip, old records read as null, the schema version, the section's rows and both lines from a fixture with a second host folder `Mac` (the fixture at `test_report.py:50-57`), the section's absence with no Mac calls, and a Mac step staying out of the Rebuilds bins.

**Constraints from prior phases:** `verify.sh` already passes `BUILDLOG_MAC` (`passed`, `failed`, `lost`, `declined`), `BUILDLOG_MAC_REASON` (`off`, `repo`, `linux_only`, `backoff`, `blocked`, `busy`, `unreachable`, `mac_busy`, `battery`, `disk`, `copy`, `local_flag`, or empty) and `BUILDLOG_MAC_S` to `record.py call`. The Mac writes the nextest step itself, with natedev's call id, host `Mac`, and no repo, worktree or sha; it reaches natedev's index with the hourly sync.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` ends `OK`; `basedpyright scripts/buildlog` ends `0 errors, 0 warnings, 0 notes`.

### Phase 6 — Live on the Mac, then switched on  · status: todo

#### Work Order

**Goal:** A real hana test run goes to the Mac and comes back right in every case, the limits are set from measured numbers, and the feature is switched on.

**Spec:**
natedev lifted its hold on the Mac on 2026-10-07 at 11:55 PDT; this phase may run there. Work in a scratch clone of hana (`git clone --local /home/natepiano/rust/hana <scratch>` at the tip of `init/catalyst`), never in another unit's worktree, with `MAC_TEST_CONFIG` pointing at a scratch copy of the config with `offload=on`. Keep saved output small and delete the clone at the end. Run the checks in order, fix each defect where it lives with a regression test, and record every number in the report back:
1. Each probe line parses on the Mac; a block makes the call stay local.
2. The first and second tree copy: times, and that `target`, `.git` and `.direnv` are absent from the mirror and the Mac's rsync accepts the options.
3. `verify.sh test hana --filter <one test>`: the Mac's cold build time, then its rebuild time after touching `crates/hana/src/tool/surface.rs`, against natedev's.
4. Exit status passes through for a pass, a failing test and a compile error.
5. An interrupt mid-run leaves no cargo, rustc or test process on the Mac and frees the claim.
6. A block placed mid-run is pending, the run finishes, exactly one "Mac is free" message arrives, and the next call stays local.
7. After `buildlog sync`, the Mac's step is in natedev's index with the call id, and the report's `Tests on the Mac` section shows the run.
8. The full `hana` and `hana_diegetic` suites on the Mac on a green tree: each test that fails only there goes into `mac_skip.hana`; a package with many goes onto `linux_only.hana`.
9. The mirror's build-folder size after both suites sets `mac_budget_gib.hana`; `free_floor_gib` is set so the Mac keeps room (it had 187 GiB free).
10. One call from inside a Codex seat's shell reaches the Mac.
Then set `offload=on` in `config/mac_test.conf` with the measured limits.

**Files:**
- `config/mac_test.conf` — `offload=on` and the measured limits.
- `scripts/mac_test/offload.py` — defects the live run finds.
- `scripts/mac_test/test_offload.py` — a regression test for each.

**Seats:** `2 writers` — nothing splits: the checks are serial against one Mac, so one writer holds the run.
- `impl` — `config/mac_test.conf`, `scripts/mac_test/offload.py`, and every live check
- `test` — opens as impl: `scripts/mac_test/test_offload.py`, a regression test for each defect `impl` reports

**Constraints from prior phases:** `verify.sh test` calls `scripts/mac_test/offload.py run` before the cargo token; `--local` skips it. `mac_test.py` has `status`, `block`, `unblock`. The report has a `Tests on the Mac` section. The mirror is `~/.local/state/mac-test/mirror/hana` on the Mac. `scripts/production/mac_run.sh` claims the same lock.

**Acceptance gate:** every check above has a recorded result; `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`; `python3 ~/.claude/scripts/mac_test/mac_test.py status` prints `free` and `no block` at the end, and no scratch clone remains.
