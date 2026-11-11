# Merge gate and test speed

Code reviews now use the unit's persistent `review` and `review2` workers
through `assign.sh --role review`. Any pool maintenance paths described below
remain only for the pending archive cleanup; they do not dispatch new reviews.

## Failed module output

Each merge gate record keeps a gzip log for each failed module attempt.
The first failure keeps its log when the retry passes.
Passing attempts and attempts stopped for a hold have an empty `log` field.
The gate removes each temporary job folder after reading its output.

`gate_modules.log` gives the saved path relative to `BUILDLOG_DIR`.
The default build log root is `~/.local/state/buildlog`.
Find the path with this command:

```sh
buildlog query "SELECT run_id, suite, module, attempt, log FROM gate_modules WHERE log IS NOT NULL AND log != '' ORDER BY started_at DESC LIMIT 20"
```

Set `log` to a path from that result, then read the output:

```sh
log='natedev/logs/YYYY-MM/<record-id>.log.gz'
zcat "${BUILDLOG_DIR:-$HOME/.local/state/buildlog}/$log"
```

`GATE_FAILURE_LOG_KIB` in `config/test_times.conf` limits the saved tail to 64 KiB by default.
The writer reads this setting for each record that has failed attempts.
The first line states how many bytes the writer cut from the start.
That line is outside the limit.
A failed log write reports an error and preserves the gate verdict.
These logs have no pruning job.
They remain until someone removes them.
Older records without a `log` field have SQL NULL in that column.

## Cloud gate route

Merge gates and targeted tests use one fresh Spot instance for each attempt after the user deploys the gate resources.
The route returns the existing `GateRun` and exit behavior.
The build queue enforces nine cloud slots and shows remote progress.

The route selects targeted modules on natedev.
The box clones the public HTTPS URL from `clone_url` in `cloud_gate.conf`.
The route bundles only commits outside natedev's `origin/main`.
It bundles HEAD and the current branch ref with `--not refs/remotes/origin/main`.
It uploads no bundle when HEAD is already on `origin/main`.
The request records the exact tested commit, tree, and natedev `origin/main` hash.
The box fetches the optional bundle and sets `origin/main` to the recorded hash.
The guest verifies the tree and runs only the supplied paths from that checkout.
The trial and route share `scripts/aws_target/gate_box.py`.
The boot script downloads the guest program before Python starts.
Production also downloads the request that contains the module list.
User data contains the download links instead of the program or module paths.
Production and trial use curl with signed HTTPS links for every box transfer.
The guest performs no type check and installs no basedpyright package.
An explicit type request and the nightly checker remain local.

The route tries all public zones at `m7a.8xlarge`, then all zones at `m7a.4xlarge`.
Both sizes use one-time Spot with termination on interruption and shutdown.
Termination deletes the root volume.
A guest deadline and an independent expiry Lambda bound normal attempt lifetimes.
The route confirms termination before it releases unused cost reservation.
A launch timeout permits local fallback and releases the cost reservation when no instance identity is known.

Every cloud setup or infrastructure failure permits local fallback.
This includes upload errors, timeouts, missing results, invalid results, and instance failures.
Capacity refusals first exhaust the configured sizes and zones.
The route releases the cost reservation and cloud slot before local admission.
Only a real remote test failure remains red.
A dead instance without a final test result permits local fallback.
The route validates source identity, attempt, modules, host, verdict, and final log delivery.
It imports the explicit `gate_run` record once under the real remote host.
Git's `cloud-gates/<attempt>` directory retains each request, bundle, launch, result, log, and import marker.
The AWS state folder retains `cloud-gate-launches.jsonl`.
Each launch call records its time, attempt, instance type, vCPUs, zone, result, error code, and reason.
A refusal prints `Cloud gate refused: <type> in <zone>: <code>`.
Local fallback prints `Cloud gate uses natedev: <reason>`.
These lines contain no account, instance, or subnet identifier.

The shared cost ledger includes Rust charges and all gate reservations.
It checks the existing $20 daily cap, $400 monthly cap, and safety reserve under one cost lock.
It splits reservations across accounting days and months.
The defaults reserve `$0.8 × 20/60 + $0.02` for each attempt.
The scaler refreshes combined Cost Explorer charges even when Rust offload is disabled.

Sessions make no AWS calls.
A user systemd service uses the scaler profile and removes inherited AWS key, profile, and config overrides.
The user alone runs the administrator deployment command:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
```

The existing Rust foundation stack must exist first.
The command adds the gate bucket, role, profile, security group, launch template, scaler policy, expiry Lambda, schedule, and log group.
It preserves Rust parameters and refuses a change set that changes a Rust resource.
It records a priced shared resource entry before creation.
It saves the image, template version, public subnets, and nixpkgs revision locally.
Gates boot the official NixOS image and prepare tools at boot.
A prepared gate image was built and then taken out of use, because it did not make gates faster.

| Scaler grant | Scope |
| --- | --- |
| `ec2:RunInstances` | Deployed image, template, security group, public subnets, gate profile, configured types, and Spot market. |
| `ec2:CreateTags` | Gate instances and volumes during launch only. |
| `iam:PassRole` | Gate role, passed to EC2 only. |
| `ec2:TerminateInstances` | This deployment's gate ownership and workload tags. |
| `ec2:DescribeInstances` | Regional describe scope. |
| `ce:GetCostAndUsage` | Combined cost query; AWS requires wildcard resource scope. |
| `s3:PutObject` | Gate source, request, and artifact prefixes. |
| `s3:GetObject` | Gate artifact, source, and request prefixes. |

The guest reads source and request objects.
The scaler signs box uploads under `artifacts/<attempt>/`; the guest role has no S3 grants.
The expiry role terminates only gate-tagged instances and writes only its function logs.
The gate bucket blocks public access, requires HTTPS, encrypts objects, and removes old artifacts.

`config/cloud_gate.conf` supplies these values through `conf.py` at use:

| Key | Default | Purpose |
| --- | --- | --- |
| `instance_types` | `m7a.8xlarge m7a.4xlarge` | Select 32 vCPU before 16 vCPU. |
| `slots` | `9` | Supply the later queue limit. |
| `lifetime_minutes` | `20` | Bound preparation, tests, and result delivery. |
| `root_gib` | `40` | Hold the disposable system and checkout. |
| `spot_max_usd_per_hour` | `0.8` | Bound the Spot price and cost reservation. |
| `attempt_allowance_usd` | `0.02` | Reserve storage, IPv4, and transfer charges. |
| `log_push_s` | `5` | Send progress logs. |
| `result_poll_s` | `2` | Check remote results. |
| `launch_reply_tries` | `2` | Recover an uncertain launch reply. |
| `api_timeout_s` | `30` | Bound each scaler API command. |
| `termination_timeout_s` | `120` | Bound termination confirmation. |
| `module_time_limit_s` | `1800` | Bound each test module. |
| `artifact_days` | `7` | Remove old S3 artifacts. |
| `log_days` | `7` | Remove old Lambda logs. |
| `expiry_schedule_minutes` | `1` | Check expired instances. |
| `expiry_timeout_s` | `30` | Bound each expiry invocation. |
| `expiry_memory_mib` | `128` | Size the expiry function. |
| `shared_usd_per_month` | `1` | Price shared gate resources before creation. |
| `credential_file` | `~/.config/aws-build/credentials` | Select the service's private scaler file. |
| `packages` | See the configured list below. | Prepare the common trial and production tools. |

The package list contains `git python3 cargo rustc rustup cargo-nextest uv curl openssh rsync gnupg ripgrep jq gcc pkg-config tmux taplo zsh bash gh sqlite chromium util-linux awscli2`.
The existing `aws_target.conf` supplies the profile, region, placement, accounting settings, and shared limits.

Fake AWS tests cover launch order, lost replies, restart recovery, expiry, termination, result validation, fallback, and concurrent cap reservations.
They also cover exact bundle delivery, local targeted selection, service isolation, record import, deployment parameters, and IAM resource scopes.
No test needs live AWS.

## Private account fixtures

`staff_test_support.py` redirects fixture sessions away from real account folders.
A real account folder is a direct child of the login home with a name that starts with `.claude` or `.codex`.
The login home comes from `pwd`, independent of `HOME`.
`fixture_account_home` compares the absolute lexical path and the resolved path.
This covers relative paths, `..` paths, symlinks, and real account folders that are themselves symlinks.

At import, the helper redirects real `CLAUDE_CONFIG_DIR` and `CODEX_HOME` values into one private temporary folder.
It keeps other folder values unchanged.
Child processes inherit the selected folders.
The temporary folder keeps each matched account folder's name and is removed at exit.

`_fixture_session_path` applies the same rule at call time, including an explicit Codex home.
It only computes a path.
Both `fixture_session` and `transcript_fixture` prepare the private file location.
A Claude write changes `CLAUDE_CONFIG_DIR` only when the folder moves.
`fixture_session` sets `CODEX_HOME` to the same selected Codex folder.
Readers use those variables to find the fixture transcripts and backend evidence.

Tests that write through another route supply their own private account folders.
The rule redirects fixture paths; it does not refuse a test operation.

`test_review_launcher.py` copies the real agent wrapper and its relative dependencies into the fixture.
These dependencies are `claude_account.sh`, `claude_result.py`, and `lib/nix_env.py`.
The wrapper therefore runs the fixture's assignment gate.

An external audit hook observes writes into real account folders.
Its `sitecustomize.py` shadows `pathlib` and disrupts `test_fn_length` and `test_repeat_guard`.
A run under that hook measures writes only; it does not establish a complete test pass.

## Model registry regression tests

The [model registry](agent-registry.md#configuration-lifetimes) defines the configuration lifetimes.
The regression tests observe complete operations and child processes.

| Test | Observation |
|---|---|
| `test_resolve_reuses_the_stored_role_and_live_model_identity` | Worker reuse reads the registry zero times with both operation snapshots cleared. |
| `test_inherited_editor_writes_without_catalog_sync` | An inherited editor publishes its row with exactly two registry reads. |
| `test_sync_codex_catalog.sh` | Lock contention produces no registry reads; an acquired lock permits reads before an invalid cache fails. |
| `test_failed_catalog_sync_warns_and_returns_the_current_configuration` | Both fresh factories warn after sync failure and return the current path and text. |

The fixtures give catalog sources fixed times and private paths.
Shell fixtures use `touch -t` to control those times.
Scratch reverts establish that each test detects its corresponding defect.

## Unit configuration fixtures

The [unit configuration writer](showrunner-automation.md#unit-configuration-formatting) owns formatting and publication.
Its `run_taplo` alias keeps the formatter independent of a fixture's replacement for `subprocess.run`.
The launcher fixture replaces non-Git commands; the formatter still runs real taplo.

## What it is

`scripts/production/merge_gate.py` merges a checkpoint into a production's merge branch and tests all modules before it pushes the merge.
It discovers every `scripts/**/test_*.py` module outside `__pycache__`.
`scripts/production/targeted_test.py --checkout TREE [--full]` runs a unit's targeted tests through the same runner.
Targeted tests select modules by direct imports, file tokens, changed test modules, and folders of changed Python source files.
File tokens can occur in a test or its imported test-support modules.
A changed shell script (a non-Python file under `scripts/` that ends in `.sh` or has an execute bit) also selects the tests that name any script that calls it, directly or through other scripts.
A script calls another when it names its path by the test rule, by a token that ends in `/` and the full path, or by a token that ends in `/` and the name in the same folder (`"$SCRIPT_DIR/board.sh"`).
Python files are not callers in this rule.
Merge gates and nightly runs use all modules.
Default merge and tests-only runs do not run basedpyright.
The daily `--nightly` run checks all of `scripts`, including hooks.
An explicit `--type-dirs scripts` runs the check by hand.
The queue reservation and the runner governor use separate configuration keys.
A clean tree with a recorded full pass on this host reuses its test modules.
Test modules use `builds.slice` scopes when available.
After the parallel pass, up to eight failed modules run once more, alone.
The final test results decide the gate.
On Linux, `module_run.py` adopts orphaned test processes in a separate parent before it imports test code.
The parent reaps adopted processes while the test child runs.
After the test child ends, the parent kills and reaps every remaining descendant, including processes in separate sessions.
SIGTERM, SIGINT and SIGHUP also start this cleanup.
The runner preserves the test child's exit status or the signal that stopped the runner.
Timing records retain test execution and interpreter startup costs; they exclude the parent's final process cleanup.
Other platforms retain the single-process runner.
A SIGKILL of the parent prevents this cleanup.
The module time limit sends SIGTERM first, so the cleanup runs.
On success, the gate pushes the merge branch.
With `--promote`, it also advances `~/.claude` main and the Mac.
On failure, it undoes the merge and prints the failed suites' output.

**Build-hold guard tests.** `scripts/hooks/test_build_hold_guard.py` calls the real Python hook entry point in-process for command parsing and hold decisions, restoring stdin, environment and import paths for each call. `build_hold_guard_test_support.py` copies the existing hold fixture's class seed into a private case directory at the same path for each test. Real subprocess tests retain the shell front's no-Python fast paths, byte-for-byte stdin/output/status relay, lazy imports, malformed input, and shell-to-reader integration. No production hook or hold decision is replaced, and no clock or wait budget is shortened.

**Build-hold tests.** `scripts/build_hold/test_build_hold.py` uses a class fixture seed copied into the same private path for each test. `build_hold_test_support.py` invokes the real argument parser and command handlers in-process for holder-state checks, while shell wait status, detached recording, lock ownership and child reaping retain real processes. Record assertions wait for recorder threads and the child-lifetime FIFO to finish instead of polling log files. Real recorder output and every original assertion remain covered.

**Mac memory retry tests.** The four cases formerly grouped in `test_verify_mac_memory_retry.py` run by outcome: a second busy decline (`memory_retry`), a successful Mac handoff (`memory_handoff`), an excluded Mac (`memory_excluded`) and a permanent decline (`memory_decline`). They reuse the committed Mac fixture's class seed, interpreter cache and event waits through `verify_mac_memory_retry_test_support.py`; each class restores its private seed at the same path. The real Bash retry, admission and process boundaries and every original assertion remain exercised.

**Dev-feature verification tests.** `test_verify_dev_features.py` covers feature merging and malformed values, `test_verify_dev_features_defaults.py` covers absent and empty settings, and `test_verify_dev_features_execution.py` covers nextest and final. Each module restores a class-level private workspace and tool seed through `verify_dev_features_test_support.py` and the existing example-workspace fixture. The seed declares its supplied toolchain and 64 GiB build budget, keeping host Nix discovery and memory admission budgets outside feature composition. Tests still run the real Bash check, nextest and final routes and assert exact Cargo arguments; malformed-value variants retain the real embedded parser. Existing consumers of the support functions keep their original behavior.

**Production lifecycle tests.** `test_production_lifecycle.py` restores a private Git origin, checkout and tool seed at the same paths for each test. `production_lifecycle_test_support.py` invokes the real lifecycle CLI in-process with its environment, working directory and import-time state restored for each call. Git pushes, rejection hooks, linked worktrees and external commands still run as real processes; a CLI parity case compares subprocess status and output. No production behavior, timing bound or original assertion is replaced.

**Offload cleanup tests.** `test_offload_cleanup.py` keeps the real sh/zsh cleanup and selector protocols; the results, leftovers and takeover modules cover the command outcomes. `offload_cleanup_test_support.py` copies a private Git and tool seed at the original paths for each case and creates fresh FIFO descriptors. Ordinary calls reuse the existing in-process CLI helper; claim-owner exit, streaming and timeout cases retain real subprocesses and the two-second timeout assertion.

**Merge gate suite tests.** `test_merge_gate_suites.py` covers discovery, isolation and reporting; the resources, tripwires, reruns, types, signals and admission modules keep each subject independently runnable. `merge_gate_suites_test_support.py` reuses the committed class seed and private copies, launches Python directly for real module jobs, and uses directory events and process descriptors for fixture barriers and cleanup. Admission arithmetic needs no Git fixture. Signal, process-group, memory-growth and timeout contracts retain real children; the controlled timeout clock and memory-growth pacing are unchanged.

`discover_test_modules` finds every `scripts/**/test_*.py` file outside `__pycache__`, in path order. The first folder under `scripts/` is its suite; a test file directly under `scripts/` is refused. `test_module_name` keeps the path within the suite in timing keys, results and log lines. Retries keep the original job path, so a nested failure reruns the same file. Job kind is independent of suite name: a test suite named `types` still runs unittests, and the type check has its own job kind. Discovery tests check real regression paths and nested retries.

**Claude launcher tests.** `test_assign_claude*.py` run the real `assign.sh` path with a private Claude binary.
The fixtures restore private run, claim, staff, transcript, and configuration files.
`test_claude_worker.py` covers the coordinator's launch, delivery, completion, and cancellation behavior.
The cancellation case permits a closed gate socket after SIGTERM.
The completion case retains its strict socket send.

**One test hang guard.** Tests import `TEST_HANG_GUARD_SECS` and `LauncherHangGuard` directly from `scripts.lib.test_hang_guard`. The shared limit is 600 seconds. `scripts/lib/test_one_hang_guard.py` parses every Python file under `scripts/` and refuses local assignments or imports through other modules. Each refusal gives its path and line. Fixture script text is data and can receive the shared value through a placeholder. Expected events use the shared guard; negative observation windows, product timeout settings and elapsed-time assertions keep their behavior.

## How it works

### Targeted tests: `scripts/production/targeted_test.py`

The entry has its own parser and description.
It shares the suite arguments, signal wrapper, copy lock, and suite runner with `merge_gate.py`.
`SuiteMode.TARGETED_TEST` enables selection and takes the `targeted-test` admission step.
`SuiteMode.MERGE_GATE` runs all modules and takes the `merge-gate` step.
Both modes accept `--full` because existing repository callers use it.

The queue model reads `config/build_steps.conf` once through `read_step_names()`.
Its `StepNames` value supplies names without file reads during rendering.
Queue tables and cards use `[names]`; only `targeted-test` has the shown name `targeted test`.
Other step IDs keep their display text.
Sentences use `[prose]`, then the shown name as a fallback.
The step column fits `targeted test`.
Build holds apply to both entries. Their test children keep the same hold rules.
Temporary folders use each step's prefix from `config/build_folders.conf`.

Each `gate_run` record carries `step`. Old records retain an empty step in the index.
A dirty checkout writes up to 50 `dirty_paths` and the total `dirty_path_count`.
The runner prints the path count.
This record changes no selection and refuses no run.

### The gate: `scripts/production/merge_gate.py`

**Invocation.** `merge_gate.py --merge-branch B --unit-branch U --checkpoint C --previous-checkpoint P --unit NAME --phase LABEL [--production <showrunner name>] [--change-review LIST] [--repo/--checkout DIR] [--body TEXT] [--type-dirs DIR...] [--promote] [--promote-only] [--main-repo ~/.claude] [--ssh ssh] [--mac-host mac] [--mac-repo ~/.claude] [--settings-blob b3466859] [--full] [--nightly]`.
The six merge flags are required unless `--tests-only`, `--promote-only`, or `--nightly` is given.
`--promote-only --merge-branch B` promotes the pushed branch to main and the Mac without merging or testing.
`--production` defaults to the calling showrunner's production, read from its update timer.
The gate refuses a merge when it cannot find a production.
After the last promotion step, it adds rows only for merges on `origin/main`.
It prints `Change review: N row(s) added to <list>: C25–C27`, then each row.
A line with `after <step>` waits for the completed `live` step.
The session runs `change_review.py live --merged <hash> --step <step> --repo <checkout> --production <showrunner>`.
`--type-dirs` (alias `--type-dir`) defaults to no folders.
A refusal prints `REFUSED: <reason>` to stderr and exits 1.
`GateArguments(argparse.Namespace)` defines the arguments; each refusal raises `GateRefused`.

**Refusals before merging** (`merge_checkpoint`):
- tracked uncommitted changes. Untracked files never block (`--untracked-files=no`), because each showrunner keeps a LOG there; a merge that would overwrite an untracked file fails and is aborted.
- checkout not on the merge branch.
- HEAD not `origin/<merge branch>` plus only local production doc commits after the fetch. `check_local_doc_commits` accepts a non-merge commit whose subject starts `production(` (`add_unit.commit_paths` makes them for add_unit and promote_showrunner, and leaves their push to the gate). Each one is printed, rolled back to on red, and pushed with the merge. Fetches use explicit `+refs/heads/X:refs/remotes/origin/X` refspecs.
- checkpoint not an ancestor of `origin/<unit branch>`.
- previous checkpoint not an ancestor of the checkpoint.

**Merge.** `git merge --no-ff` with subject `Merge <unit> <phase> (<checkpoint>) into <merge branch>` and `--body`. On a conflict the gate runs `merge --abort` and names the conflicting files.

**Run, then check before push.** `run_suites` runs. Then `settings.json` at HEAD must match the approved blob prefix (`--settings-blob`, an 8–40 hex hash), and `origin/main` must be an ancestor of HEAD. Only then does the gate push the merge branch and print `Pushed <branch>: <short hash>`.
A checkpoint rerun accepts its own recorded merge only for the same checkpoint, exact merge commit and original parents, with the remote branch still an ancestor of its base; it reruns the gate checks and pushes that commit, using a separate push recovery record rather than the test pass ledger.

**Red, errors and signals.** Any exception after the merge, including red suites and `KeyboardInterrupt`, goes to `rollback_merge(repo, baseline)`. It aborts an in-progress merge and runs `git reset --keep <merge HEAD before the merge>`. If tracked files changed during the gate block that reset, it restores them to the merge tree and retries once; if the retry also fails, it reports `merge NOT undone; merge commit <hash>; blocking files: ...`. `rollback_signals()` turns SIGTERM and SIGHUP into `GateRefused`, so they roll back the same way; SIGINT arrives as `KeyboardInterrupt`.

**Build holds.** SharedMachine waits before jobs, merging, and promotion while another session's natedev hold applies.
The gate uses `build_targets.caller_holds_only_own`, so the sole holder runs at once.
The gate checks the hold before each new job.
A job stopped by a signal during an applicable hold returns to the front of the queue.
It runs after release without using a failure rerun.
A signal without a hold fails the job; the module time limit still applies.
An empty hold directory starts no subprocess.
One line names the holder when a wait starts; another reports the wait when it ends.
`Gate tests:` excludes hold wait; a separate line reports that wait when it exceeds zero.

**Passed trees.** `${MERGE_GATE_PASSED_DIR:-~/.local/state/merge-gate}/passed-trees.jsonl` has one JSON line per passing run, with `kind: full` or `kind: affected` (a legacy record without `kind` is full). Fields are `tree` (`HEAD^{tree}`), `commit`, `at` (UTC), `host`, `checkout`, sorted `suites`, and `source` (`tests-only` or `checkpoint`). Full passes also record `reason`; older records without it remain valid. The checkout must have an empty `git status --porcelain`, including untracked files; ignored files do not count. It must still be clean with the same tree after the run. Failed runs and reused results add no record. Each append uses one write. A read or write error prints one reason and leaves the gate result unchanged; a ledger unavailable at lookup makes all modules run.

Before modules start, a clean checkout can reuse the most recent full record for the same tree, host, and sorted suite names.
Affected records certify neither reuse nor a selection base.
Commits and checkout paths may differ.
The runner prints `tests reused from <short tree> (<user-zone time>, <source>, <checkout>)` and marks each suite `OK reused`.
Explicit type checks, settings, ancestry, production doc checks, and change-review checks still run.
A dirty tree records no reusable pass.
Without exact reuse, a targeted test selects affected modules as described below.
`--full` bypasses reuse.
A red nightly marker bypasses reuse only in a merge gate.

An added or removed test folder changes the ledger suite set and prevents reuse of a pass with another suite set.

**Affected modules for targeted tests** (`scripts/production/affected_modules.py`).
The certified base is the newest full record on this host with the complete suite set and an ancestor of `HEAD`.
Other candidates are the merge bases of `HEAD` and the origin refs in `landing_branches` in `config/merge_gate.conf`.
These branches hold commits that passed a merge gate, which runs all modules.
The selector uses the candidate with the fewest changed paths to `HEAD`.
A tie keeps the certified base first, then the branch order in the config.
The selector ignores missing refs and refs without a common ancestor.
A landing candidate can replace a missing certified base.
The changed set includes changes from the base to `HEAD`, plus dirty paths.
A static Python graph selects a test module under four rules:

1. The test module changed.
2. The test module directly imports a changed Python file.
3. The test or an imported `*_test_support.py` module names a changed file through tokens.
4. The test module shares a folder with a changed non-test Python file and names its stem as a whole word.

The third rule follows imports only through `*_test_support.py` modules.
A whole path token matches any changed file, including a repository-root file.
A base name token also matches when the source shares the file's folder or contains that folder's name as a token.
Repository-root files require a whole path token.
The rule uses cached tokens and imports for Python and non-Python changed paths.
Configuration selection keeps its existing rule.

The fourth rule counts names in imports, strings, and comments.
Removed source files also count.
The selector reads that text only when no earlier rule selects the module.

Only test modules have graph rows. All Python paths can resolve imports.
A removed Python file uses import facts from the base.
A syntax error in a test module requires a full run. Non-test syntax errors do not block selection.
The selector reads source without importing or evaluating it.
The cache holds imports and complete path tokens for each test-module blob.
The current and base graphs share one cache read and one cache update.
A warm selection reads base blobs only when the cache lacks their facts.
Dirty sources never enter the cache or cause cache writes.
A targeted test runs all modules only for `--full`, no base candidate, or a graph error.
The nightly-red marker widens only a merge gate. It does not block targeted test reuse or selection.
A targeted test leaves the marker in place, including after `--full`.
`--full` bypasses reuse in both modes. The marker bypasses reuse only in a merge gate.
A passing merge gate removes the marker. An unreadable marker also requires all modules in a merge gate.

Selection prints `affected: N of M modules (base <commit>, <user-zone time>), K changed files`.
It prints module names when ten or fewer modules are selected.
A full run prints `full suite: <reason>` and repeats the reason in the final `Gate tests:` summary.
An untouched suite prints `skipped (not affected)`.
A selection of zero modules passes.
Affected passes record `kind: affected` and never replace the full base.
Types, admission, isolated reruns, and later merge checks keep their existing behavior.

**Nightly.** `merge_gate.py --nightly --merge-branch B [--production <showrunner name>] [--checkout DIR]` fetches `origin/B`.
It uses the production's standing checkout through `copies.standing(owner, repo, copies.CommittedSource(commit))`.
It runs all test modules and basedpyright over `NIGHTLY_TYPE_DIRS`, which names `scripts` and includes hooks.
The 03:00 timer calls this path.
Each exit sends `nightly.result`, or `nightly.not_run` when the checks could not start.
The result names the branch, finish time, type result and full test result.
The failure text names the reason on one line within the message limit.
Both pushes use need `note` and the key `nightly-result`.
The result requires no action. The failure text includes an action.
The gate says the next nightly run tries again. The cold credentials example directs the user to run `github-warmup`.
Silence keeps only the newest push on that key and delivers it when silence ends.
The gate reads `nightly_result_push` and `nightly_result_modules` from `config/merge_gate.conf` at use.
`nightly_result_push=off` disables both texts. The default is `on`.
`nightly_result_modules=8` limits the failed test modules named before `and N more`.
A push failure does not change the gate result, marker or showrunner notice.
A run that never reaches merge_gate.py sends no push unless the timer script sends one.
The checkout retains its ignored build files and holds the copies busy lock during the run.
The caller's local branches, HEAD and edits stay unchanged.
Git disables terminal credential prompts and uses SSH batch mode.
Promotion stays separate and runs no tests.
A green nightly records a full pass.
A failed test module or runner writes `${MERGE_GATE_STATE_DIR:-~/.local/state/merge-gate}/full-required` atomically.
The marker records the tree, UTC time and failed modules.
While the marker exists, every merge gate runs all modules until a merge gate passes.
An unreadable marker also requires all modules in a merge gate.
A failure of only the type check removes an existing marker and does not require full test runs.
Every red nightly tells the production's showrunner through the existing messaging path.
The type-failure notice includes the first 20 diagnostic lines, the count of omitted lines, and the checker summary.
Without a calling session, the gate finds the registered production for the merge branch.
`--production <showrunner name>` can name the production explicitly.
A failed delivery prints its cause. A test failure keeps its marker after a failed delivery.

**Suite runner** (`run_suites(repo, type_dirs, module_time_limit_s, max_running_jobs, *, source="tests-only", full=False, mode=SuiteMode.MERGE_GATE, machine=SharedMachine()) -> GateRun`):

`SharedMachine` is the default and keeps the existing shared scheduling behavior.
`WholeMachine` uses one job per CPU and runs every discovered module without reruns.
It uses no admission service, hold check, queue reservation, memory governor, CPU pool, systemd scope, or test ledger.
Both modes use one launch loop with the same commands, test environments, timeouts, tripwires, and process cleanup.
`WholeMachine` prints each completed job's name, exit status, and seconds.
`WholeMachine.module_seconds` supplies recorded times to `module_order`, which places unknown modules first, then measured modules from slowest to fastest.
Without recorded times, `WholeMachine` keeps the discovery order. It writes no module times file.
The gate trial sends the host's module times as a separate S3 object.
The trial host reads its root `nixpkgs` input revision from `/etc/nixos/flake.lock` before creating resources.
The guest uses that revision for Python, SQLite, Chromium, and the other Nix packages.
The guest links `google-chrome` to Chromium before the system profile bind mount for headless chart tests.
The cloud gate runs tests only, like natedev's `--tests-only` run.
It supplies no `type_dirs` and prepares no full type environment.
The nightly run does the full type check.
The guest installs no basedpyright package. The two real-tool tests have left this branch.
Root prepares the shell links and a temporary system profile bind mount with Python at `/run/current-system/sw/bin/python3`.
Before the bind mount, `cp -a` copies the system profile links and preserves their targets in `/nix/store`.
The guest runs as the unprivileged `gate-trial` user with its own home and runtime directory.
Its shell reads `/etc/profile` before tests, then restores the tool PATH.
The NixOS environment marker prevents child zsh shells from replacing fixture paths.
The guest timeline records CPU busy percent between samples from the aggregate `/proc/stat` counters, excluding idle and iowait.
The guest result records busy CPU seconds, gate seconds, vCPU count, and CPU use percent during the gate.
CPU use percent equals 100 times busy CPU seconds divided by vCPU count times gate seconds.
The trial text table shows CPU use percent for each size and run.
The admission, scheduling caches, and rerun rules below apply to `SharedMachine`.

- `test_modules(repo)` finds every `scripts/**/test_*.py` file outside `__pycache__`, sorted by path. A module belongs to its first folder under `scripts/`, including modules in nested `tests/` folders. A folder without test modules is no suite. New test folders need no registration.
- Before any job starts it refuses (and the merge rolls back) if a `--type-dirs` path is absent from the checkout, the test list is empty, or `/dev/shm` is not writable.
- Each module is one job: `python -c SUITE_BOOTSTRAP -m unittest discover -s <its folder> -p <its file>`, with the checkout as cwd, started in its own session and process group, stdin `/dev/null`. `SUITE_BOOTSTRAP` resets every signal disposition except KILL and STOP to `SIG_DFL`, then `execv`s, so a suite started from a background shell does not inherit SIGINT ignored.
- **Shared admission.** After the build hold ends, `gate_reservation` obtains one `merge-gate` reservation through `queue_place`.
  Targeted tests use step `targeted-test` through the same reservation path.
  It reads `program_first_peak_gib.merge-gate` from `config/build_admission.conf` at use; the initial value is 4 GiB.
  The gate uses the first peak as its full need.
  A matching anonymous profile uses the first pass's progress window after admission.
  Reruns publish no progress counter and keep the full need.
  A missing or legacy profile keeps the full need.
  A wait keeps its gate identity and queue place.
  An admission error refuses the gate with its last reason line.
  Each new sampled peak updates the reservation's `.max` and `.wave` files through atomic writes.
  The gate releases the reservation on every exit while its private empty-scope sidecar still exists.
  Release writes the run's `merge-gate` or `targeted-test` history row for reports.
  Admission does not use that history to size the gate.
- **Shared CPU slots.** Each module and the `types` job takes one byte from `/dev/steve` before launch.
  The gate returns the byte after the process group ends, including after failure, kill, or interruption.
  The pool accepts a FIFO or a character device, including steve's CUSE device.
  A gate with running jobs uses the existing non-blocking read.
  It continues to sample memory, collect results, and check holds.
  An idle gate with a pending job waits in a blocking read, in line with cargo.
  The gate keeps a separate blocking descriptor open for the pool lifetime.
  It prints `waits for a CPU slot` once, then prints the elapsed wait when the wait ends.
  Stop signals interrupt the wait without a masked blocking read.
  Cleanup returns all registered slots.
  Steve recovers a byte at process exit if a signal interrupts its registration after the read.
  The gate checks holds again after the blocking read.
  If a hold prevents admission, the gate returns the slot before it waits for the hold.
  An absent pool keeps the existing behavior. `BUILDLOG_JOBSERVER_FIFO` selects a private FIFO in tests.
  Launch and cleanup registration defer cancellation together. The child restores its signal mask before exec.
  The per-gate job limit still applies. Admission and token waits retain the checkout's shared busy lock.
  Memory waits print when the need or hold reason changes, with a reminder at most once a minute otherwise.
- **CPU scope.** Once per run, the gate probes `systemd-run --user --scope --quiet --collect --expand-environment=no --slice=builds.slice -- true` with a 10 s timeout. On success it puts the same prefix before each test module's argv, including reruns. The `types` job has no prefix. The scope replaces itself with the command, so the module keeps the `Popen` PID, process group and memory sampling. `builds.slice` has CPUWeight=20; the gate adds no nice value. It skips the prefix when `BUILDLOG_SCOPE=0`, `/proc/self/cgroup` contains `/builds.slice/`, or the probe fails. One line reports the scope or the skip reason.
- Each job has its own `TMPDIR` under `/dev/shm/merge-gate-*` or `/dev/shm/targeted-test-*`.
  The runner reads the output and removes the temporary folder when the job ends.
- Each test module gets a fresh `BUILDLOG_DIR` inside its temp root, so its log rows cannot reach the real build log.
- Each test module starts with tripwires for `cargo`, `rustc`, `rustdoc`, `rustup` and `cargo-nextest` first on PATH: any call, including lookup commands, exits 99 and fails the module with the tool and arguments; a module can put its own stubs first. Each module also gets a private `BUILDLOG_LIVE_MEMORY_TRIPWIRE` record path: a recorded read of `/proc/meminfo` or `/proc/pressure/memory` fails the module with `a test read live memory`, the path and the arguments, even if its tests pass. Neither kind of tripwire failure runs again, including after a signal during a natedev hold. An incomplete or invalid memory record also fails the module and prints `a test read live memory (unreadable record):` with the raw line. The types job gets no live memory tripwire.
- Exit 5 with `Ran 0 tests` and `NO TESTS RAN` counts as a pass: Python 3.13's exit for a helper module with no tests.
- A requested basedpyright job (`types`) starts first; only nightly and an explicit `--type-dirs` request it.
  `--pythonpath` selects `typecheck-venv` in the state directory.
  The venv holds the pinned `TYPECHECK_REQUIREMENTS`: pillow and fake-bpy-module dated 2026-08-22.
  These packages resolve `bpy` and `PIL`.
  The gate builds the venv with uv under a lock when it is absent or its pins change.
  A failed build makes the types job red and prints the cause and build command.
  Basedpyright then does not run.
  The job passes on exit 0 with the line `0 errors, 0 warnings, 0 notes`.
  The gate creates no `.venv` in the repository.
- `MERGE_GATE_PYTHON` and `MERGE_GATE_BASEDPYRIGHT` override the interpreter and the type checker; tests use these.
- **Ordering cache.** `${MERGE_GATE_STATE_DIR:-~/.local/state/merge-gate}/module_seconds.json` maps `suite/path/to/module.py` to the first pass's seconds. Nested paths also remain in retry and report identities, so same-named modules in one suite stay separate. Modules with no recorded time start first, then the rest longest first. The cache is rewritten atomically after every run, red ones included. `module_history.json` also records only the first pass; reruns do not change either file's module times.
- **CPU records.** `module_cpu_seconds.json` maps each module path to the last completed attempt's CPU seconds.
  The type-check job uses the `types/basedpyright` key.
  The runner reads user and system CPU time with `os.wait4` when it reaps each leader.
  Signal deferral protects the reap and its stored status and CPU value.
  This includes children that the leader waited for.
  Children killed during cleanup after their leader exits do not contribute to this reading.
  At run end, the runner writes the records atomically and retains entries for jobs that did not run.
  A job stopped at its time or memory limit does not replace or add a CPU record.
  `GateJobResult.cpu_seconds` retains the reaped CPU time for that job.
- **Rerun alone.** After all parallel jobs end, each failed test module runs once more, alone.
  Jobs killed at the module time limit and `types` do not run again.
  If more than `RERUN_LIMIT` (8) ordinary failures qualify, those failures do not run again; one line reports the count.
  Memory stops use their separate retry count even when ordinary failures exceed this limit.
  Each rerun uses the same launch, cleanup, arguments, environment, scope prefix, memory sampling, and time limit.
  A pass replaces the first failure and prints `Rerun alone: <suite>/<module> passed (first run exit N, X s)`.
  The report retains the first output under `--- <suite>/<module> passed on rerun alone; first run exit=N ---`.
  A second failure prints both outputs in attempt order.
  Suite verdicts and `GateRun` use the final status.
  Suite wall times and total time include reruns.
- **Runner governor** (`RecordedJobPeaks`, `SampledRSSAdmission`).
  `run_suites` reads `merge_gate_governor_gib` when the gate starts; the initial value is 4 GiB.
  The governor samples resident and swapped memory from `/proc` at intervals of at least 0.25 seconds.
  It measures the full job tree and each job's tree separately.
  Each job reserves its last recorded peak multiplied by `MEMORY_PEAK_MARGIN` (1.2), using records loaded before this run.
  `module_peak_mib.json` stores module peaks; `types_peak_mib.json` stores type-job peaks under the type directories.
  An unrecorded module reserves the largest recorded module peak multiplied by 1.2, or 640 MiB without module records.
  An unrecorded types job reserves 3072 MiB.
  Each running job retains `max(0, reservation − sampled memory)` as its remaining reservation.
  A new job starts when a CPU slot is free and `latest aggregate + remaining reservations + expected memory ≤ governor_mib`.
  With no job running, the governor always permits one job.
  Completed jobs update their peak records; jobs stopped at the time or memory limit do not.
  Samples do not change reservation sizes during the run.
  A test module above this limit stops, whether it runs alone or beside other jobs.
  The sample includes resident and swapped memory for the module and its descendants.
  The type-check job has no memory stop.
  `memory_stop_reruns` in `config/merge_gate.conf` sets the reruns alone after a memory stop; the default is one.
  The gate reads this count at use. Memory stops do not count toward the eight ordinary failures that prevent reruns.
  Another memory stop after the last allowed rerun fails the gate and retains the reason.
  Each stop sends sdlc a peer message with the gate, module, peak, limit, and next action.
  A failed send prints one line and does not change the gate result.
  The gate record retains the key `cap_mib` for the governor value in MiB.
- **Running job limit.** A module starts only when memory admits it and fewer than `max(1, (os.cpu_count() or 2) // 2)` jobs run: half the cores, with a minimum of one. The basedpyright job counts as one. `run_suites` accepts `max_running_jobs` for tests; there is no environment variable or command-line flag. The `Gate tests:` line names the limit once. On 2026-10-28, starting nearly all modules at once raised one-minute load from 71.6 to 339.7 and made every module 3–5 times slower.
- **Cleanup.** When a job's leader exits, the runner kills its process group to stop leaked children.
  Every exit kills and reaps running groups before removing the temporary root.
  `build_folders.temporary_build_folder` removes the private root after success, failure, TERM, HUP, or INT. It never kills a process outside its own groups.
- **Report.** Each suite shows `OK` or `RED` and its wall time.
  Failed modules show their exit codes and full output; basedpyright shows its verdict and output.
  A module reports `Slow module:` when it exceeds its recent median by both `SLOW_MODULE_FACTOR` (3) and `SLOW_MODULE_FLOOR_S` (60 seconds).
  `module_history.json` retains the last `MODULE_HISTORY_RUNS` (5) times; `module_seconds.json` supplies the fallback.
  A slow-module report does not fail the gate.
  The summary shows total time, sampled peak GiB, the largest module's peak and name, and start, maximum, and final load.
  The peak line has no governor comparison.
  After `Largest module peak:`, `Most CPU:` lists up to three jobs and total CPU seconds.
  CPU totals include all attempts, retries, hold restarts, and timeout kills.
  An empty run shows `(none)` and zero.
  `GateRun.passed` requires successful job exits and real, reused, or certified work.
  A certified empty selection can pass.

**`--tests-only`** runs all modules through `run_suites` on `--repo`, with pass recording and reuse.
It changes no Git refs or files.
It exits 0 when all requested jobs pass, and 1 when any job fails.
Checkpoint runs record their source as `checkpoint`.
Workers check their changed Python files and caller modules.

**Measured type cost.** On 2026-11-05, 214 gate runs each included a full type check.
The median type check took 164 seconds and reached 4.07 GiB.
Type checks used 3.4% of gate CPU time.
The daily rule removes these jobs from default gates.

**`--promote`** runs after the merge-branch push.

1. Wait for `build-hold-release/release.lock` within the configured release wait.
2. Verify that the promotion checkout uses main and has an eligible recorded tip.
3. Verify the settings blob and ancestry of `origin/<merge branch>`.
4. Check edited paths against the target through `promotion_edit_conflict(repo, target)`.
5. Record the exact source commit and base for push recovery.
6. Fast-forward local main and push it.
7. Fast-forward the Mac through SSH and inspect its printed `ff_rc=` result.

`promotion_edit_conflict` refuses only edited paths that the promotion also changes.
A private index exposes edits hidden by `assume-unchanged` or `skip-worktree`.
The live index and edits stay unchanged; untracked files do not count.
Local, Mac, and checkpoint promotion use this function.
A retry can push its recorded local commit when origin/main remains its ancestor and the source still contains it.
Tree equality alone does not authorize that push.
The Mac command has a 25-second limit and must print exactly one successful `ff_rc=0` result.
A failed Mac advance names the published local commit and leaves retry to main synchronization.

- **Main synchronization.** `scripts/lib/main_sync.py` fetches main and fast-forwards an eligible checkout.
  The `claude-sync` job runs every 15 minutes on both machines.
  It refuses another branch, an active Git operation, divergence, or tracked edits before an advance.
  An interrupted update is not a tracked edit. When each changed tracked file, and each untracked file new at origin/main, equals origin/main, the job finishes the update.
  It removes those untracked files, restores those tracked files to HEAD, then runs `git merge --ff-only`.
  A tracked candidate's index entry must also equal HEAD or origin/main. Any other change blocks as `dirty`, and no file changes.
  An untracked path that collides with a path origin/main adds also blocks as `dirty`: `dir` blocks an incoming `dir/file`, and `dir/file` blocks an incoming file `dir`.
  The `dirty` detail names each path that blocks the update, or the last line of each git output stream when git cannot list them.
  It never resets, stashes, or creates a merge commit.
  Each run appends `MAIN_SYNC_STATE_DIR/sync.jsonl`, default `~/.local/state/main-sync/`.
  No blocked-state marker exists.
  Each blocked run with `--notify` calls `send.py`.
  The key is `main-sync-<host>-<state>`.
  `send.conf` supplies `main_sync_repeat_minutes` for delivery suppression.
  A failed fetch advises `github-warmup` only when the credential check fails, is absent, or exceeds five seconds.
  Warm credentials instead produce a likely GitHub incident message.

Tests use independent scratch repositories.
The modules in `scripts/production/` cover these subjects:

1. `test_merge_gate.py`: checkpoints and change review.
2. `test_merge_gate_ledger.py`: certified passes.
3. `test_targeted_test.py`: affected selection.
4. `test_merge_gate_suites.py`: jobs, resources, reruns and types.
5. `test_merge_gate_promotion.py`: local and Mac promotion.
6. `test_merge_gate_holds.py`: holds and interruptions.
 `merge_gate_test_support.py` caches one repository seed per class and fixture folder set, then restores a private copy at its original path for each test. Cases declare the folders needed for their contract. The fixture writes test modules in those folders; it does not replace discovery or copy the checkout's suite names. CLI calls run in-process except where signals, child ownership or the executable entry point are under test. The gate discovers all parts through `test_*.py`; the affected-module graph follows their shared-helper imports.

Checkpoint integration modules separate inputs, receipts, validation, promotion, history, child handoffs, and handoff ledger recovery.
`merge_checkpoint_test_support.py` restores a private Git seed at the same paths for each case.
Ordinary CLI cases use the real parser and handlers in-process.
Git, executable stand-ins, and selected unit and child-handoff cases retain real subprocesses.

Findings-ledger tests remain in `scripts/direct/test_findings.py`, with `findings_test_support.py` restoring one private fixture seed at the same path for each case. Commands execute the real CLI in-process with a fresh module namespace, so every invocation rereads configuration and all ledger, event, parser and locking code stays active. Stable-ID persistence across invocations and unusable-configuration cases retain real Python subprocesses. All original test bodies and assertions are retained.

Conversation-pause hook behavior tests live in `scripts/hooks/test_conversation_pause.py`; `test_conversation_pause_processes.py` covers alarm budgets, executable exits and concurrent lock/delivery. Both use `conversation_pause_test_support.py` restoring a private fixture seed at the same path for each case. Synchronous behavior cases execute the real hook and CLI entry points and their current Python stand-ins in-process, with isolated input, output, environment, import-time fixture paths and signal handlers. Alarm-budget, concurrent lock/delivery and executable-error cases retain real processes; detached activity calls remain detached. Fixture release and activity waits use file events and a 600-second hang guard in `scripts/lib/test_hang_guard.py`. All original assertions are retained.

Style-planner tests use `style_plan_test_support.py`. Test files cover these subjects:

- `test_style_plan.py` (rank, scope and concurrency).
- `test_style_plan_phases.py` (written instructions).
- `test_style_plan_gate.py` (gate and formatting contracts).
- `test_style_plan_selection.py` (held judgments).
- `test_style_plan_ancestry.py` (judgment tips and synchronization).
- `test_style_plan_structure.py` (structure priority).
- `test_style_plan_refill.py` (unsynced product changes).
- `test_style_plan_recovery.py` (waits and interrupted phases).
- `test_style_plan_decisions.py` (decision tickets).
- `test_style_plan_batches.py` (large restructure batches).
- `test_style_plan_type_batches.py` (type read limits).
- `test_style_plan_declarations.py` (declaration following).
- `test_style_plan_decision_completion.py` (decision completion and recovery).
- `test_style_plan_sites.py` (moved sites).
- `test_style_plan_split.py` (split accounting and command orchestration).
- `test_style_plan_ownership.py` (busy rename references).
- `test_style_plan_module_owners.py` (busy module declarations).
- `test_style_plan_renames.py` (item reference edits).
- `test_style_plan_file_renames.py` (file moves and declarations).
- `test_style_plan_borrowing.py` (approval and footprint checks across owned families).
- `test_style_plan_checkpoint_refill.py` (disjoint work and two waiting checkpoints).
- `test_style_plan_pipeline_growth.py` (atomic footprint growth and rechecks).

Each class builds a complete Git/worktree fixture once and restores a private copy at the same paths for each test.
Synchronous CLI and formatter fixtures run their actual code in-process; a gate case retains the executable boundary.
Concurrent refill tests release on observed lock contention and check committed tickets and disjoint files.

### Small fixes on main

`commands/unit/fast_track.md` defines the fast-track exception for small fixes.
`fast_track.py --unit <unit> --production <showrunner name> --why "<one line>" <paths>...` selects changes against HEAD.
New files can be selected.
`copies.standing` supplies an isolated checkout of `origin/main`.
The command applies the patch with `git apply --3way`.
It runs direct test modules for changed Python files, imports each changed module, and checks the basedpyright footer.
The check uses `merge_gate.test_module_environment` and its tripwires.
An empty helper module can pass discovery; a failed package import cannot.
The fast check uses no full merge gate or queue admission.
`config/fast_track.conf` supplies the size, file, time, push-attempt, and refused-path limits.
Imports of refused repository modules also refuse the fix, including indirect imports.

`GitRunner` is the only place in `fast_track.py` that starts Git.
It keeps successful reads of objects addressed by full hash for one `reconcile` pass.
Reads of refs and ancestry stay fresh, because another process can move them.
Snapshot ref writes and recovery ref deletes use one `update-ref --stdin` batch.
`copies.refresh_remotes` fetches both mappings in one fetch.
The change took `test_fast_track` from 7,158 to 6,433 Git processes and the copies tests from 497 to 319 fetches.

A successful check creates a `fast-track(<unit>): <why>` commit with `Fast-Track: <unit>`.
`main_push_lock` protects its fast-forward push.
A moved main causes another application and check within `max_push_attempts`.
The command then promotes through the production's configured targets and restores the selected source changes.
A refusal before publication exits 2.
A failure after publication exits 1 and retains the published commit.
A completed retry prints `already on main at <commit>` and creates no new commit.

Each attempt keeps a `PushAttempt` JSON record and recovery refs under `refs/fast-track/{selections,attempts}/`.
The refs protect the original trees from garbage collection.
Automatic restore requires overlapping requested paths.
It validates every original path before restoring any path and preserves later index and worktree edits.
An unrelated pending attempt produces a warning.
`--close-restore <attempt>` permanently closes automatic restore, retains the JSON record, and removes the recovery refs.
HEAD ancestry containing the pushed commit also closes restore.
`records.jsonl` under `FAST_TRACK_STATE_DIR`, default `~/.local/state/fast-track/`, records publication and pending review.
`--pending --unit <unit>` prints pending review diffs.
Broad reviews include these diffs and record their review pass.
Later merge gates and the nightly run test published fixes through their normal paths.

`fetch_main` retries only Git failures containing `cannot lock ref`.
It uses `fetch_lock_attempts` and `fetch_lock_wait_seconds` from the command's configuration read.
Temporary checkouts use their own fetches and refs.

### Test-speed infrastructure

**`scripts/production/cli_fixture.py`**
- `invoke_cli(module, main, arguments, environment, cwd, *, combined_output=False, production_state=False) -> subprocess.CompletedProcess[str]` runs a script's real `main()`, with its parser, handlers and child commands, in the test process.
  - Inside the call it swaps `os.environ` (cleared, then set to `environment`), `sys.argv`, `sys.path` and cwd, and captures stdout and stderr.
  - Afterwards it restores those, the SIGINT/SIGTERM/SIGHUP/SIGQUIT handlers, and the module's global bindings (`vars(module)`).
  - `SystemExit` becomes the return code; a string code is printed to stderr.
  - With `production_state=True`, it first imports `scripts.production.showrunners` in the caller's environment.
    It then binds production registry paths and `note_file.AGENTS_DIR` to the fixture environment.
    The registry paths are `CONFIG`, `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR`, `SESSIONS`, and `SEND`.
- `ImmutableGitSeed.create(root, branch, author, email, files)` builds one checkout plus a bare origin per test class, with `GIT_CONFIG_GLOBAL=/dev/null`. `copy_to(checkout, origin)` copies both trees and repoints the remote, so each case has private refs, config, index and origin without re-running git setup.
- Converted modules retain a `subprocess_cli=True` case for parsing, exit status, and executable behavior.
  `scripts.mac_test.test_offload` imports `scripts.production.cli_fixture` by package name.

**Session host fakes.** Production, message, and shutdown tests use `scripts/herdr/fake_herdr.py` through `HERDR_BIN`.
They also use `scripts/production/fake_unit_host.py`.
It supplies session labels, pane reads, and a fake `/proc`.
See [herdr-adoption.md](herdr-adoption.md), Test doubles.
Production suites such as `test_waiting.py` use these fixtures.
The `ci_points` fake `gh` uses the `GH_STUB` shell and jq fixture in `test_ci_points.py`.

**`scripts/production/test_add_showrunner.py`** keeps its launch, account, trust and registry cases in one module, sharing `add_showrunner_test_support.py`. Each class builds an account/tool seed once and restores private files and symlinks at the same paths before each case. Synchronous commands run the real parser and handlers through `invoke_cli`; the fixture's herdr/systemd, registry and message scripts run in process with their recorded arguments and file effects intact. Git and the fake herdr agent-start command remain subprocesses. One complete launch case and the unset poll-default check retain their Python subprocess boundaries. Registry retries use a controlled clock, recording the requested interval; dummy session processes block on a pipe until cleanup, instead of expiring after 60 seconds.

Doc verification runs in `test_verify_doc.py` (workspace flags and judged trees), `test_verify_doc_checkout.py` (checkout and target reuse), `test_verify_doc_recovery.py` (missing or damaged indexes), `test_verify_doc_records.py` (edit detection and durable records), and `test_verify_doc_mac.py` (held-Mac routing). `verify_doc_test_support.py` builds one Git/tool seed per class and restores private copies at the same paths; standalone dev-feature consumers keep their own temporary fixture. The real Python shim uses a seeded interpreter cache, and trivial rustc/refusal tools use shell stubs. The real Bash verification and Cargo-recording processes remain because these cases prove their environment, exit status and file effects across process boundaries. There are no fixture sleeps or admission-budget waits; explicit mtimes prove checkout reuse, and the named 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs. All original test bodies and assertions are retained.

Final gate records run in `test_verify_final_pass.py` and its compiler, script, unstaged, staged, untracked, failure, edit, commit, and record subject modules. `verify_final_pass_test_support.py` builds one real Git fixture per class and restores each case at the same path, with the real Python shim's interpreter cache seeded. The tests retain Bash verification, Git, Cargo stand-ins, and read-only queries across their process boundaries. They cover clean proofs, changed compilers/scripts, dirty trees, tool failures, concurrent edits, and record pruning; all original assertions remain. Fixture memory admits immediately, there are no fixed sleeps, and command completion uses a named 600-second hang guard in `scripts/lib/test_hang_guard.py`.

Quota reminders run in `scripts/whoami/test_quota_alert.py` and nine subject modules for Claude selection/reminders, delivery/legacy state, refusals, routing, repeat routing, recovery, recovery failures, cancellation, and restoration/notices. `quota_alert_test_support.py` supplies an optional class seed of private account, configuration, and registry files; each case restores the same paths, while existing fixture consumers keep their per-test construction. Alert logic and CLI handlers already run in process. The real Bash registry editor remains for validation, routing, restoration, and process-group cancellation. Storage contention and editor cancellation use events and pipes with a named 600-second hang guard in `scripts/lib/test_hang_guard.py`; reminder clocks advance explicitly, and the fresh catalog stamp prevents unrelated catalog synchronization. All original test bodies and assertions remain.

**`scripts/direct/recorder_fixture.py`**
- `invoke_recorder(arguments, environment, cwd, *, check=False) -> CompletedProcess[str]` runs the real `progress_history.main()` through `invoke_cli`, with the recorder lock and real child integrations.
  - It patches `DEFAULT_CONFIG_PATH`, `DEFAULT_MODELS_CONFIG_PATH` and `user_zone.CONF` from the fixture HOME.
  - It clears `_config_digest`'s cache before and after the call.
  - `check=True` raises `CalledProcessError`.
  - The module imports `progress_history` and `user_zone` at load time, before any HOME swap.
- `communicate_launcher(process, timeout=TEST_HANG_GUARD_SECS) -> tuple[str, str]`: on timeout it SIGKILLs and reaps the launcher's process group, then re-raises.
- In `test_progress_history.py`, `run_command`, `run_failing_command` and `run_unowned_command` go through `invoke_recorder`.

**`scripts/direct/local_release_wait.py`**
- `local_release_waiter(holders, release) -> LocalReleaseEvents | IntervalReleasePolling`. `LocalReleaseEvents` watches the directories and their existing parents through inotify via ctypes, re-arms replaced directories, and treats queue overflow as a change. `IntervalReleasePolling` sleeps the caller's interval; it is used where libc has no inotify (macOS) or the call returns ENOSYS/ENOTSUP.
- `wait_for_local_release(seconds, call_id)` arms the watch before reading target state.
  It returns when the hold ends or the deadline is reached.
  `verify.sh` calls its script; `VERIFY_LOCAL_RELEASE_WAIT_SCRIPT` overrides the path.
  `build_targets.py` imports `scripts.direct.local_release_wait`.

**`scripts/direct/wait_for_test_state.py`**
- `wait_until(ready, directory, timeout=TEST_HANG_GUARD_SECS, *, processes=())` arms directory events, checks `ready()`, then blocks in `select` on the inotify fd plus a pidfd per child process. An exited child's pidfd stays readable, so each one is observed once and closed. Without inotify or pidfd it polls every 0.01 s. It raises `TimeoutError` at the hang guard.
- `wait_for_file(path)` is a wrapper, also callable from the command line.

### Private runtime fixtures

`staff_test_support.isolated_staff_file` supplies private staff, notifier, showrunner, session, and pane paths.
It patches loaded modules and passes the same paths to fixture children.
Checkpoint tests use this shared helper instead of live registries.
`fixture_unit_location` publishes the staff location required by a test.
Configuration fixtures supply `CONF_DIR` and the real key limit comments.
Git-recovery tests use their own committed repositories.
`test_end_session` removes inherited `CODEX_THREAD_ID` and every `PLAN_DELEGATE_*` value before it supplies its test identity.
The shared hang guard protects expected events without shortening production waits.

### Python package imports

Repository imports use `scripts.<folder>` package names, including mock patch targets.
`scripts/lib/package_root.py` places the checkout root directly after the entry script folder without a duplicate.
Entry scripts load that helper after their docstring and future imports.
Bare `python3 <path>` therefore imports the entry's own checkout from any caller folder.
A copied entry without the helper can use an existing `PYTHONPATH`.
`package_imports.py bootstrap <checkout>` adds the four-line block to new entry scripts.
It preserves CRLF and skips test and test-support modules.
The command provides no import converter.

The gate starts test modules through the checked tree's `scripts/lib/py`.
It removes the caller's `PYTHONPATH` before the shim sets the checked tree's package root.
The gate refuses a tree without that shim.
`MERGE_GATE_PYTHON` selects the test interpreter.
For script paths, the shim selects the script's checkout.
For stdin, `-c`, and `-m`, it selects its own checkout and passes `-P` before other Python arguments.
Outside the gate, it retains inherited Python paths after that root.
It preserves the caller's working directory, interpreter cache, version floor, and `CLAUDE_PYTHON` override.
`module_run.py` and `fast_track.py` load tests by package name.
The cloud guest bootstrap supplies its own root after a fresh clone.
The Pillow analysis and Blender entries retain their foreign interpreters and explicit `PYTHONPATH`.

`update_registration.state_block` imports `merged_checkpoints` in its body.
This keeps `scripts.production.merge_checkpoint` and `scripts.production.merge_gate` out of each `scripts.statusline.counts_row` start, which costs 0.1 to 0.2 s of CPU when they load.

**Interpreter-cache seeding.** `scripts/lib/py` caches the resolved interpreter path in `$XDG_CACHE_HOME/claude/python3-<major>.<minor>`. The key uses the version floor from `lib/py`, currently `python3-3.12`. Worker fixtures set `XDG_CACHE_HOME` for each temporary HOME. They write `sys.executable` into that cache file. They unset `CLAUDE_PYTHON`, which bypasses the cache. This avoids Python resolution for roughly 19 helper launches per `verify.sh`, `review.sh`, or `assign.sh` run.

**Stub app-server shutdown** (`scripts/agents/codex_test_support.py`). The accept loop blocks in `select` on the listener and on a socketpair that `close()` writes to, so closing the server wakes it at once.

### Wait and sync overrides

| Override | File | Production default | How tests use it |
|---|---|---|---|
| `UNIT_EARLY_POLL_SECS` | `scripts/direct/review.sh` | `5` (s) | `0.01`; unset-default test sees one `sleep 5` |
| `UNIT_WORKER_POLL_SECS` | `scripts/direct/claude_worker.py` (worker polls) | `CLAUDE_WORKER_POLL_SECS=15` (s), from `config/direct.conf` | `0.01` |
| `UNIT_TOKEN_CLOCK_FILE` | `scripts/direct/verify.sh` | unset → `$SECONDS` | a file the test advances; unset-default test measures from `$SECONDS` |
| `UNIT_SYNC_WRITES` | `progress_history.py`, `findings.py` | `1` (fsync) | `0`; each has a test with it unset |
| `--durability {full,off}` (`LedgerDurability.DURABLE`/`DISPOSABLE`) | `scripts/usage/collect.py` | `full` → `PRAGMA synchronous=FULL` | `off`; default pinned by an SQL trace |
| `ADD_SHOWRUNNER_POLL_S` | `scripts/production/add_showrunner.py` (`POLL_S`, read at import) | `2.0` | `0.01`; default test imports with it unset |
| `leftover_timeout_s` (`OffloadConfig`, settings file) | `scripts/mac_test/offload.py` | `10` s | small or large per case; invalid values fall back to 10 |
| `UNIT_RUN_ROOT` | `end_session.sh` (passed to `unit_runs.py held --root`) | both run roots, `/tmp/claude/unit` then `/tmp/claude/delegate` | a temp dir |
| `MERGE_GATE_STATE_DIR`, `MERGE_GATE_PYTHON`, `MERGE_GATE_BASEDPYRIGHT`, `MERGE_GATE_UV` | `merge_gate.py` | `~/.local/state/merge-gate`, `sys.executable`, `basedpyright`, `uv` | scratch dir and stubs |

The build hold reader finds the run owner through `StaffRow.run`.
Tests use a private `STAFF_FILE` and private run folders.
The build hold guard checks the state overrides listed in `override_guard.py` and `.sh`.
An installed script exits 2 when a listed override is set or `HOME` differs from the account home.
Tests use the checkout's scripts.

Before the rename, each listed `UNIT_*` name had the prefix `PLAN_DELEGATE_*`.
`UNIT_RUN_DIR` had the name `PLAN_DELEGATE_BOARD_DIR`.
Runs that started before the rename still export the former names.
Readers retain those names while such runs can remain open.
The Bash scripts `review.sh`, `verify.sh`, `board.sh`, `end_session.sh`, and `memory_gate.sh` source `scripts/lib/unit_env.sh`.
That file exports each unset `UNIT_*` name from its former name.
Python readers use `unit_runs.environ(<name>)` or read the former name after the new name.
`unit_status.sh` reads the former name inline.
The status line reads assignments from staff rows instead of these environment names (`showrunner-automation.md`).

## Invariants

- **No test reaches live state:** `~/.codex*`, `~/.local/state/`, live run folders, a real herdr server, the real `codex app-server`, and phone sends. Test discovery never starts a real session host or builds a missing host binary.
  - Production quota tests supply `CodexAlertPassReadings` (`scripts/whoami/quota_alert.py`), and a patched `observe_workers_home` raises on any fallback to a live Codex home.
  - Sockets use private temp dirs or ephemeral ports. No test depends on a fixed port: the refused-start test binds an ephemeral port and closes it.
  - No test writes into the repo.

- **No wall-clock thresholds.** A timing claim is a work count (launches in flight, subshells traced with a DEBUG trap, barrier arrivals), a controlled clock (token clock file, injected durations), or a comparison calibrated in the same run (buildlog report: CLI CPU at full and quarter log size, three runs each). Fixture deadlines use the one 600-second hang guard in `scripts/lib/test_hang_guard.py`; they are not pass/fail limits.

- **Selection requires certification.** Unaffected modules may be omitted only by the dependency selector against a full certified ancestor. Full runs still cover every module; no test is disabled, no assertion weakened and no tier removed from the full gate. A red under load is fixed at its source: an event-driven wait, a drained pipe, an injected `TimeoutExpired`, a hang guard.

- **The gate finds test modules recursively.** Every `scripts/**/test_*.py` file outside `__pycache__` is part of discovery. Checks that require an installed system use a different file name. After an install, run the installed-hook check with `cd ~/.claude/scripts && ~/.claude/scripts/lib/py -m unittest berth.tests.check_installed_hooks`.

- **Override defaults stay as they are.** Each default is pinned by a test that runs the real script or module with the override unset and observes the default through behavior: the sleep argument, the attempt count, the clock source, the fsync/PRAGMA call, the computed module value. No test reads a literal from source. A new override needs the same pin.

- **CLI contract kept.** A script converted to in-process tests keeps one subprocess test for parsing, exit status and launch. A moved test replaces its subprocess form; it never sits beside it with the same inputs and assertion.

- **No trivial tests.** A test is trivial when it cannot fail for a real defect: (1) it asserts a constant, literal or default against itself; (2) it repeats the implementation; (3) it tests the standard library or a mock it configured; (4) it only checks that a name exists or a module imports; (5) it duplicates another test's inputs and assertion. A short test that pins behavior a user or another script depends on is not trivial; when unsure, keep it. `docs/direct/write_prompt_contract.md` section 6 (`## Work Specification`) carries this as one line copied into every worker prompt that writes tests.

- **Phone-send guards stay.** `whoami/test_quota_alert.py::test_setup_blocks_real_phone_sends` and `lint/test_sweep.py::test_base_blocks_real_phone_sends` pin the fixture patch that stops real sends.

- **Signals.** A test that signals a child restores `SIG_DFL` before exec and waits for a ready marker written after the child installs its handlers (for example `runner-signals-ready`).

- **Fixture cleanup.** A fixture that times out reaps its process group and owned threads (`communicate_launcher`) before restoring the app-server fixture or HOME.

- **In-process cases are synchronous only.** `invoke_cli` suits scripts that do not mutate containers held in module globals. Cache-holding functions (`_config_digest`) are cleared around each case.

- **The gate kills only what it started:** its own process groups, never by pattern.

## Calibration / gotchas

| Item | Value |
|---|---|
| Queue reservation | `program_first_peak_gib.merge-gate=4`, with the progress window after admission |
| Runner governor | `merge_gate_governor_gib=4`, read when the gate starts |
| Recorded job reservation | Last sampled peak × 1.2 (`MEMORY_PEAK_MARGIN`) |
| Unrecorded module reservation | Largest recorded module peak × 1.2, or 640 MiB without module records |
| Unrecorded types reservation | 3072 MiB |
| Memory sampling interval | 0.25 s or more; scheduler loop sleeps 0.05 s |
| Fixture hang guards | The one 600-second hang guard in `scripts/lib/test_hang_guard.py` (`wait_until`, `communicate_launcher`, script launches, follow tests) |
| Agents quota-handoff start timeout | 30 s, so socket round trips under load never race the five-second task deadline |
| Mac fast-forward | ssh, 25 s limit |

Representative gate runs on natedev (32 cores):

| Run | Wall time | 1-min load | Peak |
|---|---|---|---|
| Sequential | about 30 min | | |
| Parallel | 75 s | 6–22 | 2.3 GiB |
| Parallel | about 101 s | 30–58 | 2.54 GiB |
| Parallel | 150–195 s | 64–123 | 2.3–2.5 GiB |

The largest module peaks near 0.5 GiB. The gate's wall time is its slowest module, dependent on load and the changed test modules. The Mac verify tests run by subject in `test_verify_mac_*.py`: options (`offload`, `local`, `local_arguments`), fixture safety (`fixture`, `pressure`), configuration (`configuration`, `configuration_errors`, `result_paths`), results (`results`, `malformed`, `result_shape`, `result_status`, `failed_status`), routing (`fallback`, `fallback_announcements`, `fallback_diagnostics`, `arguments`, `routing`, `routing_changes`, `wait_records`, `holder_history`, `pacing`), release (`release`, `local_release`, `release_events`, `withdrawal`), retries (`lifecycle`, `memory_retry`, `memory_retry_clock`, `retry_clock`, `retry_hold`), copy races (`copy_results`, `copy_ownership`) and signals (`signals`, `signal_boundaries`). They share `verify_mac_offload_test_support.py`. Each class builds one private tool seed, restored at the same path before every test. The seed supplies the complete toolchain without host Nix discovery and matches its memory budget to its private 64 GiB reading; zero-memory and PSI-pressure tests still block real admission. File and child-exit events advance the fixtures; real Bash routing, token ownership, admission, copy watching and signal delivery remain exercised. Synchronous Mac claim commands run in process while their signals to copy subprocesses stay real. Token tests run in `test_verify_token_wait.py` (acquisition and clock defaults), `test_verify_token_queue.py` (supersession), `test_verify_token_fifo.py` (queue order and indefinite waits), `test_verify_token_history.py` (token-wait records), `test_verify_token_memory_history.py` (memory-wait records), `test_verify_token_build.py` (build arguments and binaries), `test_verify_token_check.py` (check argument ordering), `test_verify_token_release.py` (step ownership and release failures), `test_verify_token_detach.py` (worker detachment), `test_verify_token_ownership.py` (reclaim and signals), `test_verify_token_handoff.py` (memory and hold handoffs), and `test_verify_token_reservation.py` (reservations across token waits), sharing `verify_token_wait_test_support.py` and retaining real Bash, board and admission checks. Each token class builds one tool seed and restores a private copy at the same path for each test. The seed pins the Python interpreter cache and supplies the complete toolchain without host Nix discovery. Its 48 GiB memory reading and build budget admit both 12 GiB fallback promises without an unrelated 120-second ramp wait; zero-memory cases still block real admission. Output waits subscribe to log modifications, and memory and hold stubs wait for release events. These subjects are sized by summed child CPU (about 100 seconds or less per module), because CPU work spread across parallel modules lowers the gate floor; poll sleeps use immediate state rechecks or release events. Times taken at different loads are not comparable; compare at matched load.

Token ownership tests use `verify_token_ownership_test_support.py` on the unchanged shared token fixture. Each class restores its private seed at the same path. Subjects run in `test_verify_token_ownership.py` (live expired holder), `test_verify_token_same_slot.py` (overlapping generations), `test_verify_token_reclaim.py` (dead holders and old builds), `test_verify_token_signals.py` (step interruption), and `test_verify_token_cancellation.py` (waiting-call cancellation and portable cleanup). Real Bash, board acquisition, Cargo stand-ins, process groups and signals remain exercised. Linux test observations read live group/session metadata directly instead of spawning machine-wide `ps` scans. The existing sleep stand-in publishes a failed board acquisition then execs the same real sleep; acquisition and child-exit events replace the cancellation test's fixed polling. Waits use the one 600-second hang guard in `scripts/lib/test_hang_guard.py` under load.

Hold waits run by subject in `test_verify_hold_wait.py` (admission and command counts), `test_verify_hold_history.py` (phase attribution), `test_verify_hold_workers.py`, `test_verify_hold_director.py`, `test_verify_hold_mac.py` (remote bypass), `test_verify_hold_lint.py` (lint admission and width), `test_verify_hold_steps.py` (between-step accounting), `test_verify_hold_signals.py`, `test_verify_hold_events.py` (notification and fallback contracts), and `test_verify_hold_memory.py` (memory/hold ordering). They share `verify_hold_wait_test_support.py`, which reuses the Mac tool contract and builds one seed per class, restoring a private copy at the same path for every test. The complete fake toolchain and private memory budget avoid host discovery and unrelated admission delays. Real Bash verification, board ownership, admission, history writers and signals remain exercised; synchronous recorder and hold operations run in process. Their private Python shim directly execs the selected interpreter or recorder observer, omitting repeated version probes while preserving real script processes. Completed calls drain without repeated process scans; the original final marker scan still checks detached children before fixture cleanup. The release helper subscribes to nested waiter publication and cancellation events instead of polling.

Lint scope runs in `test_verify_lint_scope.py` (workspace and package fix/judgment commands), `test_verify_lint_selection.py` (check-only, feature refusal and examples), and `test_verify_lint_output.py` (shell output, tee failures and skipped commands). They retain every original assertion and the real Bash verification and invocation libraries. `verify_lint_scope_test_support.py`, also used by the lint cache, edits, fixes and formatter suites, builds one private tool seed per class and restores files and environment at the original paths for each test. Standalone fixture users retain their own temporary lifecycle. The fixture supplies Cargo metadata, fake Cargo/Taplo/Git tools and private memory readings, and marks that toolchain ready rather than rediscovering the host Nix environment. Interpreter selection still uses the real cached Python shim. There are no fixture sleeps, poll intervals or unrelated admission waits to advance; the 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs. The tests prove shell process boundaries, so those commands remain subprocesses.

Step routing runs in `test_verify_step_routes.py` (admission and refusals), `test_verify_step_routes_lint.py` (formatting and Mac judgment), `test_verify_step_routes_helpers.py` (helpers after a local mend), `test_verify_step_routes_results.py` (declined, lost and invalid Mac replies), and `test_verify_step_routes_restart.py` (stopped local steps). They share `verify_step_routes_test_support.py`, which reuses the committed Mac fixture contract and builds the route tools once per class, restoring a private copy at the same path for each test. All original assertions, fake-tool observations and the final child-leak check remain. Real Bash routing, admission, record transitions and tool processes remain because the tests prove their process boundaries. Holds change at fake-tool events rather than elapsed intervals; private memory and interpreter state avoid unrelated admission and discovery waits. The 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs. The original module still exports the route stubs for token-wait tests.

Unit status tests cover registered units, Claude liveness, tick health, user gates, and routing.
The modules are `test_unit_status.py`, `test_unit_status_decisions.py`, and `test_unit_status_routing.py`.
They share `unit_status_test_support.py`.
It builds one script tree per class and restores private copies at the same paths for each case.
Shell dispatch tables supply exact fake-herdr replies and argument logs.
The real status shell, production lookup, session registry, and pane readers remain subprocesses.
Registry processes wait for pipe EOF during cleanup.
The 600-second limit in `scripts/lib/test_hang_guard.py` detects hangs.
Dailies parsing and its separate test module were retired on 2026-11-11.

Example verification runs in `test_verify_untested_examples.py` (clean package) and `test_verify_example_acceptance.py` (accepted examples), `test_verify_example_markers.py` (test attributes and module files), `test_verify_example_manifest.py` (manifest refusal and diagnostic ordering), `test_verify_example_cache.py` (new tests invalidate passes) and `test_verify_example_edits.py` (edits during tests and session writes), `test_verify_example_dependencies.py` (dependency-scoped invalidation), and `test_verify_example_commands.py` (targeted tests and the removed verb), and `test_verify_example_final.py` (final command ordering). They share `verify_examples_test_support.py`, which seeds the private tools once per class and restores a copy at the original paths for every test. The seed pins the interpreter cache, marks its complete toolchain ready and matches its memory budget to its 64 GiB reading. Real Bash verification, Cargo metadata calls, command ordering and pass-cache writes remain exercised. Existing users of the unseeded workspace fixture keep their original lifecycle and environment through compatibility exports; the 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs.

- **Inherited SIGINT.** A suite started in the background of a non-interactive shell inherits SIGINT ignored, and bash cannot forward a signal it ignores. `SUITE_BOOTSTRAP` resets dispositions for gate jobs; a signal test still restores `SIG_DFL` itself.

- **The governor controls starts.** A job can exceed the governor after admission.
  A memory peak does not kill the job or fail the gate.
- **Module time limit.** The gate kills a module that reaches `DEFAULT_MODULE_TIME_LIMIT_S` (1800 seconds, `--module-time-limit-s`).
  Its result starts `killed: still running after 1800 s, the module time limit`, and the gate fails.
  This prevents a stalled module from holding every gate behind it.

- **macOS has no inotify.** `local_release_wait` and `wait_until` fall back to interval polling there.

- **pidfds stay readable** after the process exits; `wait_until` observes each one once and drops it.

- **Import-time paths.** A module computes its default paths when it is imported.
  Import it before changing HOME to a fixture path.
  `invoke_cli(production_state=True)` and `recorder_fixture.py` preserve caller paths this way.

- **Bindings, not containers.** `invoke_cli` restores module bindings, not mutable containers in module globals.

- **Shell override defaults.** Launcher and token tests remove test overrides when they check configured defaults.
  Claude launch checks use the current `test_assign_claude*.py` fixtures.

- **Mac ssh status.** The Mac ssh command always exits 0; read `ff_rc=`.

- **Red-suite reruns.** The gate retries eligible failed modules once, alone, as described above. A later full-suite rerun is the showrunner's rule.

- **fsync on tmpfs is already cheap.** `UNIT_SYNC_WRITES=0` matters on disk-backed TMPDIRs; `test_progress_history`'s remaining cost is process launches.

- **`build_targets` is already at 4 launches per module run.** Further launch cuts belong elsewhere.

## Why

- **The cost was process launches.** Measured `setUpClass` cost was negligible; production ran about 26,000 execs (about 15,000 git, about 4,000 Python) and worker about 22,000. Each fix therefore removes launches (in-process CLI, per-class git seeds, shell fakes, interpreter cache) or removes fixed waits, rather than reworking setup.
- **One job per module, not per suite.** Whole suites in parallel are bounded by worker (about 7.5 min). Per-module jobs are bounded by the slowest module, so further speed comes from shortening the slowest modules.
- **Separate memory settings.** The queue reservation controls admission of the gate.
  The governor controls parallel job starts inside the gate.
  Recorded peaks and remaining reservations prevent small startup readings from permitting too many jobs.
  The empty-run exception ensures that a job larger than the governor can start.
- **In-process CLI over subprocess.** A `python3 <script>.py` launch cost 380–440 ms, about 150 ms of it import. Running the real `main()` in-process keeps the parser, handlers and child commands real; the one subprocess case per script keeps the launch contract covered.
- **The interpreter cache is fixed in the fixtures, not in `lib/py`.** The cache misses only because tests point HOME at a temp dir; production resolution was already correct.
- **One shared restore implementation.** Worker reuses `scripts.production.cli_fixture.invoke_cli` through `recorder_fixture` rather than keeping its own copy; one implementation of environment, signal and binding restore is simpler to keep correct.
- **Overrides, not shorter production waits.** The production polls and timeouts are tuned for real agents and machines. Tests set a variable, a module attribute, a config value or a clock file; production behavior stays the same and is pinned by default tests.
- **Event waits over polls.** inotify and pidfd waits wake on the change itself, so tests are both faster and immune to load, where a fixed poll or deadline gives only one of those.
- **The phone-send guards are kept.** They look like tests of a mock, but they pin the patch that stops a fixture from sending real phone messages.
- **Trivial tests were rare.** The audit of 5,713 tests deleted five and moved no gate time; the criteria live in the worker prompt contract so new ones are not written.

Registration tests use `update_registration_test_support.py` and split into `test_update_registration.py` (timer registration), `test_update_registration_commands.py` (copied executable and usage-error boundaries), `test_update_registration_log.py` (time, log and scratch-state migration), `test_update_registration_review.py` (question review), `test_update_registration_questions.py` (stored questions and reports), `test_update_registration_open.py` (listing and urgency), and `test_update_registration_answers.py` (filing, answers and locking). Each class builds one Git fixture and restores a private copy at the same paths per test. Ordinary registration CLI calls and the current Python notifier stub execute their real code in-process with environment, argv, cwd and import-time paths restored; Git and copied-command boundary tests retain subprocesses. The list-lock test proves a real child holds the exact lock with a nonblocking acquisition, then releases it through a pipe instead of sleeping. Original assertions remain, with the elapsed-time lock assertion replaced by direct contention and acquisition assertions. The 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs.

The Bevy upgrade CLI tests (`scripts/bevy_upgrade/test_cli.py`) build one synthetic checkout, lockfile, upstream fixture and tool seed per class, then restore private copies at the same path for each test. Behavioral cases run the real parser and handlers with isolated process state and network rejection. The current Python tool fixtures run in-process, preserving their record reads, lock probes, event ordering and failures; the start case retains a real CLI interpreter and both tool wrappers. Save/fetch tracing still checks the real record lock and single-save behavior. No sleeps, polling or scheduler budgets are involved.

The unit-transfer tests use `transfer_unit_test_support.py` with private staff, Git, configuration, and terminal fixtures.
They check parent changes, retained worktrees, unchanged configuration bytes, tab reconciliation, and repeated transfers.
The tests also retain staff-keyed stall files across the transfer.

Lint cache checks run by subject in `test_verify_lint_cache.py` (fixed and formatted trees), `test_verify_lint_cache_fix_limit.py` (a passing judgment at the repair limit), and `test_verify_lint_cache_mend.py` (mend rewrites). `verify_lint_cache_test_support.py` reuses the committed lint-scope fixture, including its one seed per class and private copies at the same paths. It enables pass records through `UNIT_RUN_DIR` with no team role, so cache checks do not also open a director Cargo token or progress window. The real Bash verification, cache keys, invocation policy and existing fake tools still run as processes. Every original assertion remains; waits use the one 600-second hang guard in `scripts/lib/test_hang_guard.py`.

Usage collector tests use `collect_test_support.py`, which builds one source fixture per class and restores private copies at the same paths per test. `test_collect.py` covers local collection; the Mac reader, role evidence, stream recovery and process lifetime checks run in `test_collect_mac.py`, `test_collect_mac_roles.py`, `test_collect_mac_recovery.py` and `test_collect_processes.py`. Ordinary CLI calls and their empty watcher fixture run in-process; explicit watcher handoff, durability tracing, pass-lock children, fake SSH and the real remote reader retain their process boundaries. Socket readiness and release events replace the Mac lock test’s marker polling. A pipe signals when the local record iterator resumes after processing attribution; the fake SSH then checks the committed role snapshot through its own SQLite connection. Existing assertions and timing bounds remain; the one 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs.

`test_verify_tree_cancel.py` keeps source and external-document coverage; lint,
progress-history, token reacquisition and Mac cancellation live in
`test_verify_tree_cancel_{lint,history,reacquire,mac}.py`.
`verify_tree_cancel_test_support.py` seeds the full Git workspace once per class
and restores private copies at the same absolute path. The real verifier,
watcher, Git edits, process descendants, cancellation signals and original
completion bounds stay in use. Fake lint rewrites wait for parent acknowledgments,
which also check that judgment watching has not begun; memory transitions use
file events. Simple tool replies use shell builtins, and session cleanup reuses
the token ownership fixture's process metadata observer.

Lint repair ordering runs in `test_verify_lint_fixes.py` (successive fixes before judgment, with cache off and on) and `test_verify_lint_fix_failures.py` (formatting after failed repair and preservation of the final judgment status). Both reuse the committed lint-scope fixture, which builds once per class and restores private copies at the same paths. The convergence test inherits `VerifyLintCacheFixture`, enabling pass records through `UNIT_RUN_DIR` without an unrelated director token or progress window. Real Bash verification, source hashing, fake Cargo/Taplo/Git processes and every original assertion remain. There are no fixture sleeps or polling intervals; the existing 600-second hang guard in `scripts/lib/test_hang_guard.py` only guards hangs.

Tree watcher scope coverage remains in `scripts/direct/test_tree_watch.py`, with `tree_watch_test_support.py` owning one Git fixture seed per class, including the two nested Cargo workspace layouts, and private copies restored at the same paths per test. Key CLI calls run in-process; scope tests run the real watcher loop with an event-controlled sleep, completing explicit snapshot iterations instead of waiting through fixed intervals. All Git snapshots and file publication stay real, and the parent-exit test keeps a real watcher process. Negative checks require two completed observations and a live watcher, followed by the original positive-change proof; the original 2.5-second change and parent-exit bounds remain. Only non-speed setup and cleanup waits use the named 600-second hang guard in `scripts/lib/test_hang_guard.py`.

2026-11-11: Dailies input, rendering, and their dedicated test fixtures were retired.
Use [/showrunner:units](../../commands/showrunner/units.md) to read current unit status.

## Cloud gate resource names

The feature uses `scripts/aws_target/cloud_gate.py` and `config/cloud_gate.conf`.
The machines use the name `cloud-claude-gate`.
The instance `Name` tag and the resource `Workload` tags use `cloud-claude-gate`.
The instance profile has no CloudFormation tag property.

| Resource | Name |
| --- | --- |
| Bucket, role, profile, security group, launch template | `cloud-claude-gate-${DeploymentId}` |
| Expiry role, expiry function, schedule | `cloud-claude-gate-${DeploymentId}-expiry` |
| Log group | `/aws/lambda/cloud-claude-gate-${DeploymentId}-expiry` |

The box sends logs every `log_push_s=5` seconds.
The route reads module counters from each log download.
It writes the existing queue progress format through `progress_profile.publish_counter`.

The route admits with `CloudPlace("aws")` before it requests a box.
The route uses its own pid as the run identity.
The build queue releases the slot when the route ends.
The route passes the reservation sidecar path to the scaler service.
The service writes module progress and `CloudProgress` beside that path.
The cloud state records the phase, vCPU count, failed module count, and update time.
The phases are `requesting`, `setup`, `running`, and `uploading`.
The route releases cloud admission before local fallback requests memory.
The route requests no local memory for a cloud gate.

## Verify function tests and process lifetime

`scripts/direct/shell_function_test_support.py` supplies `shell_functions(path, *names)`.
It extracts named top-level Bash definitions in the requested order and refuses a missing name.
Fixtures must supply each extracted function's current dependencies from their owning modules.
For example, `run()` needs `buildlog_step_compiles` from `scripts/lint/invoke.sh`.
A missing helper produces status 127 and does not test production behavior.

Direct tests call real production functions when no process boundary is part of the assertion.
`NextestArgumentTests` calls `compose_nextest_args` with the real metadata helpers.
The real Mac-argument runs retain decline-to-local, busy-result, and plain-package `no_tests` coverage.
The `no_tests` run observes `--no-tests=warn` at both consumers and counts one call at each.
A composed argument alone cannot prove what its consumer receives.

`call_mac_functions` supplies private tools and records for real result and retry functions.
`run_unblocked_step`, `invoke_taplo_at`, and `start_owned_step` provide direct calls for their subjects.
The lint judgment tests call `run_lint_judgment`, `run_lint_held_sequence`, and `run_lint_sequence` directly.
`FirstJudgmentRepairTests` calls `first_judgment_permits_repair` with a private log.
Real verifier cases retain status 1 and signal-status coverage because the hold-stop path depends on status.
A retry-deadline check retains its real routing path when a direct call would miss a removed consumer check.
The measurement method and module costs remain in [Verify test cost](../plans/natedev-verify-test-cost.md).
Compare paired runs under the same load; isolated wall-time changes do not prove a CPU improvement.

Each fixture that reaches repository setup provides a root `Cargo.toml` at its temporary Git top level.
A no-manifest case calls Bash directly because the inherited `verify()` helper reads the manifest before Bash starts.
A subprocess with no `cwd` inherits the caller's repository.
Script-copy fixtures must also copy the helper's current library dependencies.
DEBUG hooks that observe commands inside moved functions require functrace.
A hook that matches `BASH_COMMAND` must match the current command text.
Otherwise the test can wait until its hang guard without reaching its observation.

`VerifyHoldWaitFixture.fixture_cleanups` includes each failed inner cleanup's traceback.
`reap` reports each remaining PID with its command line.
`assert_nothing_outlives` fails with those identities after its cleanup attempt.
`wait_for_marker_exit` observes marked process exit; reaping remains a separate operation.
`start_memory_queued_lint` parks a real verifier through private zero-memory admission for signal tests.
A sleep stand-in accelerates only the intended admission poll, while renewer sleeps retain real delays.

`local_hold_environment` restores only keys that it changed.
It does not clear the whole environment while another thread can read it.
Both hold-environment contexts use `hold_recorder_exits` before restoring environment or removing the fixture root.
The helper records each `hold-event-recorder` thread as `HoldRecorderStart` before its `start()` returns.
It waits for startup, then joins only threads with an assigned `ident`.
On timeout, it kills recorder children and reports `fixture hold recorder never exited`.
A detached hold recorder does not carry the normal test process marker.
Without this wait, a late write can make directory removal fail with `ENOTEMPTY`.
The next fixture copy can then fail because that directory still exists.
Deadline tests watch private folders, so another test's event cannot end their wait.

Mac signal-boundary tests control both sides of `MAC_RUNNER_PID=$!` and include TERM and INT.
They wait for log removal before permitting the delayed runner or tee to continue.
Their final checks require the signal status, no runner or local work, no token record, and an empty temporary folder.
Fixed sleeps and retries cannot replace these ordering checks.
A group-signal test uses `scripts.lib.process_group.send_process_group`.

`OffloadSettingsTests`, `OffloadCommandFixture`, and `RepositorySlotTests` use `copies_environment` for private mirror leases.
`COPIES_CONFIG` selects that root.
`CONF_DIR` and a patched `Path.home` cannot replace `COPIES_CONFIG` for this purpose.
The Linux fake `df` selects units from option arguments only.
A letter `G` in a mirror path must not select GiB output.
`LinuxShellTests` need a usable rustup toolchain because they reach real rustc.

The renewer-boundary test observes the exact sleep PID assignment.
Only the renewer's sleep may stop; inherited jobs must survive.
Memory-growth reproductions use a bounded scope, such as `MemoryMax=3G` with swap disabled.
`step_selection.py` must remain cheap because every target command imports it.
`parse_port_cell` imports `cell_value` locally, so progress-history startup avoids unrelated production and message imports.

The merge gate owns module folders through `temporary_build_folder` contexts.
The contexts remove folders after success or an exception.
Style lint uses the same context and keeps its standing Cargo target.
Style review keeps its input folder because the review worker reads it after preparation exits.


## Synchronous verify helpers

`verify.sh` shares one Python process across synchronous helper calls.
See [Synchronous verify helpers](build-targets.md#synchronous-verify-helpers) for the protocol, process exceptions, recovery, and trace format.
