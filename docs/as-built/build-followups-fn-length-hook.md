# Unit tooling: function-length hook, showrunner question hook, stall watcher and launcher follow-ups

## What it is

A set of agent-session tools in `~/.claude`, all Python (standard library) or zsh/bash, with tests beside the scripts:

- A PostToolUse hook that blocks an editing agent the moment an edit leaves a Rust function over clippy's `too_many_lines` limit.

- The showrunner question Stop hook was retired on 2026-10-19.
  The user retired its reply footer on 2026-10-18.
  Dailies and their adhoc-review pause were retired on 2026-11-11.

- Cargo-token hygiene: each verify step runs in its own process group that a release or reclaim stops.

- Launcher changes: `assign.sh --role implement --to <worker>` sends more work to an open worker of the same family. `review.sh --to <worker>` does the same for reviews. See `long-lived-workers.md`. A Claude worker starts at its registry effort. A summary from a pane uses plain text. `/unit:report on|off` switches a unit's progress updates.

The 96-hour measure ran from 2026-10-07 01:44:05 EDT to 2026-10-11 01:44:05 EDT. It found 27 `too_many_lines` failed steps among 763 hana clippy steps. The rate was 3.539 per 100 against a ≤3.3 limit: **FAIL** (baseline 13.3). Sole-cause cost was 0.116 worker-hours per day against ≤0.27: **PASS** (baseline 1.07). All four Claude/Codex controls on natedev/Mac passed.

The [multiply-add lint fix](build-followups-mul-add.md) and the [one-command unit start and checkpoint merge](showrunner-automation.md) have their own docs.

### Function-length hook

`scripts/hooks/fn_length_lib.py` holds the edit parser, Rust scanner and Cargo lint reader. `scripts/hooks/post-tool-use-fn-length.py` is the hook entry. `scripts/hooks/test_fn_length.py` checks its behavior.

Claude's `Edit|MultiEdit|Write` PostToolUse group runs basedpyright and fn-length only. The fn-length command is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"`.

- **Payloads.** One entry serves both agents. `applied_rust_edits(raw) -> AppliedRustEdits | IgnoredEdit` returns one of two states. `AppliedRustEdits(agent, tool, cwd, files)` holds the Rust paths left by one supported edit, in payload order. `IgnoredEdit` means no Rust path has final contents to check. Claude `Edit`, `MultiEdit` and `Write` supply a `.rs` `tool_input.file_path` and give `agent="claude"`. Codex `apply_patch` supplies patch text in `tool_input.command` and gives `agent="codex"`. `applied_patch_rust_files` reads `*** Add File:` and `*** Update File:` paths. A `*** Move to:` header replaces its update path with the destination. The parser filters the final paths for `.rs` and keeps the first occurrence of each path. It ignores deletes and converts patch CRLF to LF. The hook checks each whole file. The entry returns before importing the parser when the raw text holds neither `.rs` nor `\u`.
- **Check.** `long_functions(rs_file: os.PathLike[str] | str) -> tuple[TooManyLinesLintScope, list[FunctionLength]]` returns the non-exempt functions over the threshold, in source order; empty when the scope is disabled or the file is unreadable or not UTF-8. `TooManyLinesLintScope(enabled, threshold)` and `FunctionLength(name, line, lines, exempt)` are plain `__slots__` classes. The whole file is checked, not only the touched lines.
- **Verdict.** One JSON object: `decision: "block"`, `reason`, `continue: true`, `systemMessage: "fn-length: <summary>"`, and `hookSpecificOutput.additionalContext`. The reason is `fn <name> at <path>:<line> is <N> lines (limit <T>)` joined by `; `, then `: split it now. The edit was applied.`; `<path>` is relative to `realpath(cwd)` when inside it, else absolute. It never names the exemption attributes.
- **Log.** Every block appends one record per affected file to `blocks.jsonl` under `FN_LENGTH_HOOK_STATE` when set, else `~/.local/state/fn-length-hook` (directory mode 0700): `{"at", "agent", "tool", "cwd", "file", "threshold", "functions": [{"name","line","lines"}]}`. Both agents log, so a re-measure can prove both fired.
- **Lint scope.** `read_clippy_lint(rs_file, lint, group) -> ConfiguredClippyLint | UnavailableClippyLint` reads the named lint from the nearest package manifest. `ConfiguredClippyLint(level, package_dir)` holds the effective level and package directory. `ClippyLintLevel` has `ALLOW`, `EXPECT`, `WARN`, `DENY`, `FORBID` and `UNSET`. `UnavailableClippyLint.NO_PACKAGE` means no package manifest was found. `UNREADABLE` means a read or parse failed. Manifest text with no `lints` gives `UNSET`. With `lints.workspace = true`, the reader follows `package.workspace` or searches ancestors for `[workspace.lints.clippy]`. An explicit `too_many_lines` entry takes precedence over `pedantic`. The reader accepts a string level or a `{level = …}` table. The hook fires at warn, deny or forbid. It searches from the package directory through its ancestors for `too-many-lines-threshold` in `clippy.toml` or `.clippy.toml`. The default is 100. A value that is not an integer, is a boolean, or is negative disables the check. Unreadable or invalid configuration also disables it.
- **Counting.** `count_body_lines(body)` is clippy's loop: strip the body, split on newlines, and count a line only when code appears outside a comment, carrying block-comment state across lines (`/*` before `//` enters a comment, `*/` leaves it). Strings are not parsed, so `"a // b"` counts when code precedes it. A line of `code /* c */` counts; the signature, attributes and doc comments never do. Threshold 100 means 101 lines fail.
- **Scan.** `measure_functions(source, *, skip_example_tests=False) -> list[FunctionLength]` tokenizes with one regex (`_TOKENS`: line and nested block comments, strings, raw strings, char literals, `#[`/`#![`, delimiters, `!`, and the keywords `fn impl trait mod`), pairs delimiters, and walks tokens once. `body_opener` skips parenthesized and bracketed runs and gives up at `;` or `}` (no body). Names come from `_function_name` (raw identifiers `r#x`, comments between `fn` and the name). Closure lines count in the enclosing body; a nested `fn` is also measured on its own. Macro bodies (`macro_rules!`, `name! { … }`) are skipped via `macro_before`; a keyword is never a macro name and `!=` is never a marker.
- **Exemption.** `_is_exempt` accepts `allow(…)`, `expect(…)` or `cfg_attr(…, allow|expect(…))` naming `clippy::too_many_lines` or `clippy::pedantic`. Outer attributes pending since the last `;`, `{` or `}` attach to the next item: a `fn` is exempt, and everything inside an `impl`, `trait`, `mod` or `fn` block is exempt. `#![…]` exempts the rest of its block. A keyword inside a signature (`-> impl Fn()`) is not an item. With `skip_example_tests`, `#[cfg(test)] mod` blocks in a package-root `examples/` target are skipped.

### Codex registration and trust

`scripts/hooks/codex_hooks.py install|check` manages twelve handlers.
The `queue-start` and `queue-tools` handlers run `codex-queue-drain.py` at `SessionStart` and `PostToolUse`.
Both use matcher `*` and 10-second timeouts.
`docs/as-built/session-roster.md` describes these handlers.
Fn-length uses `PostToolUse` on `apply_patch` with a 10-second timeout.
Berth uses `PreToolUse` on `apply_patch` with a 30-second timeout and runs `pre-tool-use-berth.py`.
The scratch guard and build-hold guard use `PreToolUse` on `Bash`.
The scratch guard reads `guard_timeout_seconds` from `config/copies.conf`; the build-hold guard uses 10 seconds.
Codex uses `Bash` for shell commands, as Claude Code does.
The guards run `copies/copy_guard.py` and `pre-tool-use-build-hold-guard.sh`.

The `compaction-marker` handler runs `pre-compact-marker.py --family codex` at `PreCompact`, with matcher `*`.
It records the input `session_id` before manual or automatic compaction.
The installer lists this event as `preCompact` and records its trust separately.
The `compaction-clear-turn` handler runs `compaction-marker-clear.sh` at `UserPromptSubmit`.
The shell command starts no Python process when the marker folder has no `*.json` file.
The `compaction-clear-end` handler runs `compaction-marker-clear.py` at `PostCompact`.
Both handlers delete only the input session's marker through the shared clear function.
Codex 0.167.0 lists these events as `userPromptSubmit` and `postCompact`.
It reports no matcher for `UserPromptSubmit` or `Stop`, so listing checks use `null` for those events.
See [compaction markers](hook-dispatch.md#compaction-start-markers) for the marker format and retention setting.

The `turn-end` handler runs `../herdr/turn_end.py` through the shared Python launcher at `Stop`, with a 10-second timeout.
Codex supplies the event as JSON on stdin.
The handler sends the payload to a separate child through stdin and returns without stdout text.
The child applies the worker completion rule.
The handler never exits 2, which would request another Codex turn.
The installer lists this event as `stop`, with a null matcher.
See [worker completion](long-lived-workers.md#pane-submission-and-completion) for the completion rule and deployment steps.

The `ste` and `ste-subagent` handlers run `session-start-ste.py` through the shared Python launcher, with 10-second timeouts. They use `SessionStart` and `SubagentStart`, respectively. Both use `matcher: "*"`. For `SessionStart`, the matcher filters the session source, including startup, resume, clear and compact. For `SubagentStart`, it filters the subagent type. The wildcard covers every source and type. See the [Codex hook reference](https://learn.chatgpt.com/docs/hooks).

`session-start-ste.py` reads `config/simplified-technical-english.md` from its own checkout on each call. It prints the full text as `hookSpecificOutput.additionalContext`, with `hookEventName` set to the input event. Claude Code can use the same script for both events. A missing or unreadable file produces no output and exits 0. Invalid input also exits silently. The rule file is the single source; the hook does not copy its text into code.

Both `install` and `check` return 0 only when every handler is trusted and enabled.
A trusted, enabled duplicate takes precedence over another matching entry.
Selection compares event, matcher, command, handler type, and timeout through one shared field definition.
The two STE events share a command but retain separate trust identities.
`hooks/list` reports their events as `sessionStart` and `subagentStart`.
Claude registration in `settings.json` and Codex installation are separate deployment steps.
Claude's `SessionStart` registration runs `session-start-ste.py` with matcher `startup|resume|clear|compact`.
Its `SubagentStart` registration runs the same script.
On natedev and the Mac, `~/.codex`, `~/.codex1`, and `~/.codex2` share one `hooks.json`.
Each home must trust its twelve handlers separately.

Codex 0.167.0 requires integer `timeout` values in `hooks.json`. A floating value such as `10.0` rejects the whole file, including fn-length, STE, and queue handlers. The installer rounds configured timeouts up to whole seconds. It also rounds existing floating timeouts up to integers in every handler, including user handlers. It keeps every other field of a user handler unchanged. This preserves their groups and indexes. The listed `timeoutSec` is then an integer. No older-version matching rule is used.

`CodexHook(name, script, event, timeout)` defines a required handler. `LocatedHook` holds its definition, key, current hash, trust state and enabled state. `MissingHook` means no matching handler was listed from the managed configuration.

`install` reads `CODEX_HOME/hooks.json`, or `~/.codex/hooks.json` when `CODEX_HOME` is unset. It appends missing groups and writes through atomic replacement. A symlink keeps its link and updates its target. The installer uses `codex app-server` to initialize, list handlers and trust each untrusted or modified managed handler. Each trust write uses `config/batchWrite` with a `hooks.state` upsert and `reloadUserConfig: true`. It lists again after each write. `check` only lists and reports status. File or RPC errors return 1. A failed command names its action and reason on stderr. The installer reports Codex listing warnings and errors. A missing handler reports its command or the field values that differ. An untrusted or disabled handler reports `trustStatus` and `enabled`. Each app-server session has a 30-second deadline. Its child stops when the session closes. Binary and home overrides resolve before the child changes directory.

`install` also removes a command handler when its `command` equals `RETIRED_COMMAND` exactly, with any timeout. The constant is:

```text
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-mul-add.py"
```

The installer keeps each group in place, even when empty. This keeps other groups' indexes stable. It leaves commands that only mention the script, use a different path, or add shell text unchanged. If any remaining command handler names that script, it prints one stderr warning that names the configuration file and asks for removal by hand. The warning does not change the exit status.

Removing a handler can move a sibling to a new handler index. Before rewriting the configuration, the installer lists this file's trusted, enabled entries. `HandlerTrustIdentity(event, matcher, command, timeout, current_hash)` records the contents to which that trust applies. Boundary values with no matcher, command or timeout become `AllToolsMatcher.ALL_TOOLS`, `AbsentHandlerCommand.NO_COMMAND` or `DefaultHookTimeout.DEFAULT`.

After the rewrite, the installer lists the entries again. It selects each now-untrusted entry whose identity and hash match a recorded trusted entry. It restores all selected hashes at their new keys in one `hooks.state` upsert batch. A previously untrusted or disabled custom handler gains no trust from this step. The installer lists again after a restore. A Codex trust key holds the group and handler index. Its trusted hash covers the event, matcher and handler, but not those indexes.

An install that removes nothing makes no extra app-server calls for cleanup. A second install with no other changes calls only `initialize` and `hooks/list`. Synthetic app-server tests cover sibling trust, disabled and untrusted siblings, exact command matching, warnings, group indexes, other configurations and repeat installs. Tests never run the real Codex or write the real Codex home.

### Showrunner question hook

The user retired the reply footer, its `Waiting on:` block, and `/showrunner:footer` on 2026-10-18.
The user retired the question Stop hook on 2026-10-19.
The user reads counts in the status line.
The remaining readers supply production questions for that line.

Files: `scripts/hooks/showrunner_footer.py`, `scripts/production/footer_counts.py`, the Open-questions list and The hook bullets of `commands/showrunner/produce.md`. The Stop hook that checked a showrunner's reply is gone (2026-10-19).

- **Production.** Production identity and question paths come from the staff record.
  The user's zone setting supplies the zone.
  Dailies schedules no longer supply production status.
- **Open questions.** `open_questions(productions) -> int | None` counts the items now due in every production's needs-user file: an item with no `after`, or whose `after` is at or before now in the production's zone. It is `None` when a file cannot be read.
- **Counts line.** `scripts/production/footer_counts.py` reads each count at render time and stores none. `changes` uses the staff routing function in `scripts/production/change_routing.py`, which also supplies the change page tabs.
The router reads the fixed staff id in each row's Producer cell.
It follows the unit's staff parents to its showrunner.
An empty Producer cell belongs to the list's showrunner.
An unknown id or a broken parent chain counts under `unrouted`.
The status line shows that count as a separate item.
This global count includes the same lists as the page across independent production roots.
The page, footer, and `/next` use the same typed waiting rule.
The status count watches staff, lists, and delivery records.
The merge writer and `change_review add` store Producer ids.

`proposals` counts `proposals_table.waiting_entries(doc)`, the same production rows as `/next proposal`.
These include open proposal issues awaiting approval and proposed lines in the Improvement queue ledger.
Accepted and deferred plan items do not count.
`proposals_folder.owners` and `proposer` filter shared issue folders by the showrunner and its units.
Each showrunner counts its own proposals.
`sources()` includes the Improvement queue ledger, so a change refreshes the status line cache.
An unreadable source contributes zero.
`counts_line` prints `For you: 1 question, 2 changes, 130 efficiency, 1 proposal` and omits zero counts.
It returns `None` when all counts are zero.
Every kind is available through `/next`.
The status line includes open questions from `open_questions` through `showrunner_footer.counts_line`.
Replies carry no count line.
- **Old state.** Switch files left in `~/.local/state/showrunner/footers-off/` are not read; `tidy_wrapped.py` deletes a production's file at wrap. Pause records still load. Their footer keys have no effect. Shutdown restart keeps only staff row IDs.

### Tool-output read guard

Files: `scripts/hooks/pre-tool-use-tool-output-read.py`, `scripts/hooks/test_tool_output_read.py`. It goes in `settings.json`'s `Bash` PreToolUse group, beside the heredoc guard.

- **Why.** A session's large tool results are saved under `~/.claude*/projects/**/tool-results/`, and `settings.json` already allows the Read tool there. Claude Code asks before it runs `awk`, or a sed script with a `$` or `/re/` address, and that prompt waits on the user. Probes in default mode on 2026-10-18 found these prompt: `sed -n '5,$p'`, `sed -n '$p'`, `sed -n '/a/,$p'`, `sed -n '/a/,/b/p'` and `awk 'NR>=5'`. These do not: `sed -n '1,5p'`, `grep -n`, `grep -n -A3`, `head -5`, `head -40 | tail -5`, `tail -n +5`, `cat` and `wc -l`.
- **Check.** It acts only on a Bash command that names a path matching `\.claude\d*/projects/\S*tool-results/`. `segments` splits the command with `shlex` (punctuation `();<>|&` and newline) into simple commands. A simple command whose program is `awk`, `gawk`, `mawk` or `nawk`, or `sed` with any script other than `N` or `N,M` then `p` (each `-e` value, else the first operand), is refused with a `deny` that names the Read tool, `grep -n`, `tail -n +N`, `head -N | tail -M` and `sed -n 'N,Mp'`. A command `shlex` cannot split, a non-Bash call and input that is not JSON pass.
- **Changes and copies.** `changes_output` refuses a simple command whose program is `rm`, `cp`, `mv` or `tee` with an argument matching the same pattern, and a redirect operator (`>`, `>>`, `&>`, `>|`) whose target matches it. These also prompt, and units waited hours on an `rm` of the saved `cargo-berth board --json` output (2026-10-21). The `deny` says to leave saved output alone and names `cat <file> > /tmp/claude/<name>` to keep a copy. `commands/unit/direct.md` now reads only `.status` from the board through `jq`, so that output is not saved.
- **Not an allow rule.** A `Bash(sed -n …)` or `Bash(awk …)` allow rule would also allow `sed -n 'w <file>'` and `awk 'BEGIN{system(…)}'`, so the guard sends the session to the forms that already run with no prompt.

### Account usage rate

- `scripts/whoami/run_out.py`: `Reading(at, used_percent)`, `read_readings`, and `weighted_rate(readings, end) -> float | None`: percent used per second by `end`, each rise between consecutive readings weighted by 0.5^(age / `HALF_LIFE`, 12 h). A drop (a weekly refill or a redeemed reset), time at 100% and a gap over `MAX_GAP` (15 min: the account sat logged out) are skipped, so rises before a refill still count. None until the counted time, weighted, reaches `MIN_SPAN` (1 h).

### Showrunner settings and stall watcher

- `config/showrunners.json` (git-ignored; `SHOWRUNNERS_CONFIG` redirects it) holds five settings: `threshold_percent` 2, `repeat_minutes` 30, `stall_minutes` 5, `faults_to` `natedev` and `always` `["natedev"]`. `quota_alert.py` sends to `always`, then to each running showrunner by its name now, each name once.

- `scripts/production/stall_watch.py` runs each minute as the run-only notifier instance `stall-watch`. Per tick, under a non-blocking `flock` on `~/.local/state/stall-watch/lock` (a held lock exits 0 at once): for each running showrunner (`registered_showrunners()` with a `socket`), for each unit that `unit_lookup.labeled_sessions(<showrunner name>)` finds with a `LiveClaude`, take its pane and that Claude's pid, session id and socket, and track a stretch. A showrunner that is not running has no one to report to, so its units are not watched.

- Every unit with a live Claude is watched.
  - A stretch is `Stretch(pane_hash, since, bump_sent, tell_sent, reported_status)` in `~/.local/state/stall-watch/<sha256(showrunner staff name NUL unit id)>.json`. The hash covers `session_pane.read_place(place, 400, joined=True)` on the unit's herdr pane. A changed pane resets it unless something was sent; transcript activity (the session's `.jsonl` and its `subagents/` files) moves `since` forward before a report is sent.
  - **Permission prompt (2026-10-18).** Before the turn-end check, `session_pane.agent_status(place)` reads herdr's `agent list` state for the unit's pane. `blocked` means a trust, permission or question prompt waits on the user, so no bump can move it: the idle stretch starts again, nothing goes to the unit or the showrunner, and `<stretch>.prompt.json` keeps `{since, alerted}`. After `PROMPT_ALERT_SECONDS` (60) the user gets one phone alert, the registered text `stall_watch.permission_prompt` (`push_texts.user_send_argv`, need `blocked`), with the unit, the machine, the time and `prompt_request(pane)`. Its body is "{unit} asks to {request}. It has waited since {since}." and then the lines "Yes: it does that and goes on." and "No: it does not, and asks you what to do instead." (2026-10-23). Read from the screen, the request is "edit panes.py" from "Do you want to make this edit to panes.py?", "run a shell command to <description>" from a Bash prompt, else the question's own words. A failed send is sent again on the next tick. A prompt that shows Claude Code's auto-mode banner (`AUTO_DENY_BANNER`, a line that starts "⚠ Claude Code will automatically deny this request in M:SS") gets no alert and drops its `<stretch>.prompt.json`, since Claude Code denies it by itself when the countdown ends; `prompt_request` skips that line, so the description stays the real one (2026-10-19). With one-minute ticks the alert arrives one to two minutes after the prompt shows. An unreadable herdr state counts as not blocked, ends no prompt, and is reported on stderr.
  - **Retire the alert when its prompt is gone (2026-10-23).** The alert is an emergency push: Pushover repeats it every 300 s for 3 h until the user taps Acknowledge, so on 2026-10-23 an alert sent at 09:04 AST went on after the user answered the prompt at 09:22. It is sent with the per-unit key `stall-watch:prompt:<showrunner name>:<unit>` (`prompt_key`), which pushover.py tags it with. When the prompt is gone after its alert went out (`alerted` true), `end_prompt` marks `<stretch>.prompt.json` `retiring` and queues `send.py retire --to user --from stall-watch --key <key>` (`push_texts.user_retire_argv`) in the `send_all` batch, kind `retire`, under the same 90 s deadline. The prompt is gone when the unit is no longer blocked, when its prompt shows the auto-deny banner, when its run is no longer running (`forget`), and when its Claude is not running. A different pending call while the unit stays blocked also retires the old alert first, since both alerts have the one key; the new prompt can alert after the retire, independently of case creation. Exit 0 ("cancelled" or "nothing to cancel") removes the prompt state or, if its case is still pending, atomically moves the saved snapshot to `<report hash>.case.json` in the same state directory for case retries. Exit 3, a timeout or a failure to start keeps it with `retiring` true and prints to stderr: the next tick sends the retire again and no new alert, also when the unit is blocked again. A prompt that never alerted sends no retire; its state is removed. The alert's rca.py case keeps the wait's start, the sender `stall-watch` and this key (`prompt_report`), so `rca.py fixed` retires the alert too.
  - **Request and wait start from the transcript (2026-10-21).** `pending_tool(session_id)` reads the newest tool call with no `tool_result` yet, from the end of the session's transcript and of each helper (subagent) transcript, `projects/*/<session id>/subagents/agent-*.jsonl`, since a helper's prompt shows in the session's pane (2026-10-23). `prompt_request(pane, pending)` words it: a Bash call is "run <command>", with whitespace collapsed to single spaces and each path with a folder shown as its file name, cut at `COMMAND_LENGTH` (90: 89 characters and "…"), as send.py cuts a refused command (2026-10-23). The description is never used, since the user decides on the command that runs. Edit, MultiEdit, NotebookEdit and Write are "edit <file>" or "write <file>". Without a pending call the screen is read: the Bash prompt's description is its sentence line plus its wrapped lowercase lines, and the warning paragraph is never the description. When that prompt shows Claude Code's warning "<path>, which is outside the allowed working directories", the request names what the command does to that file instead ("delete its saved tool output b1e4yc8b9.txt, which is outside its allowed folders"; `rm`, `cp`, `mv` give delete, copy, move, any other program "use"; a file in the `tool-results` folder is "its saved tool output"). `<stretch>.prompt.json` also keeps the call's id as `tool`: the wait starts at the call's transcript time, not at herdr's first `blocked` reading, and a different call is a new prompt with its own alert. On 2026-10-21 herdr reported cargo-liner-style-types idle from its 10:54 prompt until 16:21 (herdr owns that), so the alert said "since 16:21" and cut the request at 200 characters.
  - Staff supplies the unit's identity, parent, current session, and retirement state.
    The watcher inspects the director's held unfinished run.
    The [stall-watch contract](showrunner-automation.md#invariants) defines waiting statuses, outside parties, and compaction wakes.
    The watcher leaves an outside wait quiet and resets the idle stretch when work resumes.
  - `context_near_compaction` uses the shared context hook's bounded transcript read and model-specific threshold.
    Missing usage retains the waiting rule.
    Permission prompts and running work retain their separate handling.
  - Idle for `stall_minutes` with no work and a `holding:` status naming an outside party: only a per-unit log line, with no bump or showrunner tell. The hold leaves `tell_sent` false. A later idle unit can receive its bump and tell in the same stretch. Other idle units, including holds naming only their own work and units with no turn-end status, keep one bump to the unit's own socket (`stall-watch: you have been idle since HH:MM <zone> with nothing running. Continue your run; …`, key `stall-watch:<unit>:<since>:bump`) and one report to its showrunner (`<unit> idle since …, nothing running; bumped. Last status: <line>`, key `…:tell`) per idle stretch.
  - `send_all` starts every `send.py --to uds:<socket> --from stall-watch --key <key> --text …` together and waits at most 90 s for the batch; a failed recipient is retried next tick with the same key; the stretch flag is set only after a successful send. A successful prompt alert immediately saves `alerted` and its report as `case_pending`. `retry_cases` opens pending cases after the batch and on later ticks, clearing each report only when its case opens or joins; case retries send no second alert. A cleared prompt retires its alert even while the case is pending. Successful retirement moves a pending case to its own `.case.json` snapshot, freeing the active prompt state for new alerts. `retry_cases` reads both prompt and case files and removes each case file only after its case opens or joins.

- `notifier.sh new <instance> --every <min> --run <cmd> [--timeout <s>]` creates a run-only instance with `RUN=` in `conf`.
  It cannot combine `--run` with `--to`, `--from`, `--check`, `--command`, `--prompt`, `--hold` or `--aligned`.
  The timeout defaults to 120 seconds.
  `cmd_tick` starts each due job through `launch_run` outside the tick.
  Linux uses a transient `systemd-run --user --collect --quiet --no-block` service named `session-notifier-<name>-<pid>-<random>`.
  macOS uses a `launchctl submit` job that runs `run-due-and-remove <name> <label>` and removes its own label.
  Without either launcher, the notifier logs `run launcher unavailable: <name>; waiting after tick` and runs the job after it releases the tick lock.
  The job claims its slot and replaces `<instance>/run.log` with its command output.
  A `zselect` watchdog sends TERM at the timeout.
  The job appends `run timeout` or `run exit <rc>` to `fire.log`.
  Other jobs run inside the tick.

### Production launch and wrap records

`scripts/production/add_showrunner.py` writes `~/.local/state/showrunner/<owner staff id>/add_showrunner.json`.
The receipt stores `owner`, `caller`, `resume` and `started_at`.
It stores no `session_id`.
A pending receipt also stores `doc` and `config_dir` as creation claims.

`finish_publication` completes an unknown home through `staff.set_home(..., only_unknown=True)`.
The setter checks the current home inside the staff transaction.
A stale receipt cannot overwrite a home that another writer records first.
Publication then removes `session_id`, `doc`, and `config_dir` from the receipt.
The first launch and a repeated owned launch with creation claims both call `finish_publication`.
This order lets a repeated launch complete an interrupted home write.

`completion_receipt.read_launch_receipt` resolves `owner` through staff and returns `OwnedReceipt`, `UnpublishedReceipt` or `NoReceipt`.
An owned receipt uses the owner's current session, document and home from staff.
An unpublished receipt has no staff row and uses `owner` as its native session identity.
Its creation claims supply the document and account directory until publication completes.
`ShowrunnerLaunch.from_receipt` accepts the receipt variant and reads `started_at` from its fields.

`scripts/production/production_lifecycle.py` saves `wrap_release.json` under the same owner directory.
`former_parent` stores a recipient staff id or `null`.
`former_children` and `told` store lists of recipient staff ids.
`showrunner_tree.edges_to_release` captures these ids from staff relationships.
`release_edges` reads each recipient's current staff name immediately before its delivery.
After a successful delivery, it saves that recipient's id in `told`.
It reads current names again for the final parent and children report.
This per-delivery lookup preserves correct routing when a recipient changes name during an earlier delivery.

### Adhoc review

Dailies and their pause/resume steps were retired on 2026-11-11.
`/adhoc_review` no longer manages a dailies schedule.

### Cargo token and step groups

- `verify.sh` takes the token with `board.sh acquire <dir> <slot> cargo --pid $$ --hold 3600`, with no time limit, and retains it during each memory wait (see build-memory-admission.md). Each cargo step runs under `STEP_OWNER_PY`: the parent forwards INT/TERM/HUP to the child's group; the child calls `os.setsid()`, and under `cargo.guard` writes `{"group", "start"}` (pid and `/proc` start-time identity) to `locks/cargo.d/owned` only if `holder_pid` still equals the verify pid (a missing `holder_pid` counts as not equal; else exit 125 with `this run lost the cargo token before its step started; run it again.`), then runs the command.
- `board.sh cleanup_owned_group` runs on `release` and on dead-holder reclaim: it checks the recorded leader's identity, `SIGKILL`s the group, waits 2 s, and fails closed (`board.sh: step group still has running processes`, `… leader identity changed`, `cannot inspect step process group`) with the token left in place. An expired lock whose holder is alive is reclaimed without touching its build; the reclaimed holder is appended to `reclaimed_holders`.
- `board.sh release <session_dir> <agent> <resource> --pid <pid>` refuses a pid that is not the recorded holder's: exit 3 when the caller is in `reclaimed_holders` (the new holder owns the token), else exit 1, reason on stderr. Without `--pid`, a token whose `holder_pid` is still alive is refused with exit 1 (`<agent> gave no pid and <resource> is held by live pid <pid>`): the launchers' closing `release` names only the role, which two phases of one session share, so it frees only a gone holder's token. `verify.sh`'s `release_token` always passes `--pid $$`; 0 and 3 count as released, any other status turns a passing run into exit 1.

### Launcher follow-up path

The director uses `assign.sh` to dispatch each worker turn.
The [worker design](long-lived-workers.md#launchers) owns launcher claims, availability checks, status files, and compaction.
Its [handoff path](long-lived-workers.md#family-handoff-and-retry) restores context during a family switch or pending delivery.
The staff row supplies the current session and worktree.
Run files keep staff references instead of copied native identities.

### Worker effort, pane summary, `/unit:report on|off`

- **Removed 2026-10-19.** `unit_notifier.sh`, `/unit:report on|off` and the `prepare_session.sh` instance step below no longer exist: unit reports come only when asked (`docs/as-built/session-notifier.md`, "Unit reports").

- `assign_claude.py` passes the effort and dispatch role directly to `scripts/direct/claude_worker.py`.
  A nonempty effort adds `--effort <level>` beside `--model`.
  An empty registry effort prevents the caller's environment from supplying another effort.
  A continued worker keeps its effort.
  The module records the dispatch role before delivery.
- The [Claude coordinator](long-lived-workers.md#claude-coordinator) proves the running session before publication and delivery.

- When a worker leaves its summary file empty, `scripts/direct/claude_worker.py` writes a summary from the pane log in the assign process.
  It decodes UTF-8 with replacement.
  It removes OSC sequences (`ESC ]` to BEL, `ESC \`, or the end of text).
  It removes CSI sequences (`ESC [ [0-?]* [ -/]* [@-~]`) and other escape sequences (`ESC [ -/]* [0-~]`).
  It removes control characters `\x00-\x08`, `\x0b-\x1f`, and `\x7f-\x9f`.
  It retains newlines, tabs, and the last 4000 characters.
  With no readable text, the summary is `The background agent <name> produced no summary.`
  It preserves the log and any summary that the worker writes.
- The director's staff row records its open run folder in `run`.
  `unit_runs.open_run(session_id)` reads that pointer.
  `direct_run.py check` uses the pointer to identify the current run.
  The notifier instance uses `unit-<run id>` or the former `delegate-<run id>` prefix.
- `/unit:report [tables|on|off]` (`commands/unit/report.md`): `on` or `off` runs `zsh ~/.claude/scripts/direct/unit_notifier.sh "$CLAUDE_CODE_SESSION_ID" on|off`, relays its line and composes no report; a failure is relayed as printed. A Codex unit has no notifier, so the command says the switch is unavailable there.

## Invariants

- The fn-length hook passes edits with no `Cargo.toml`, unreadable TOML, unknown payloads or internal errors. Unclosed function bodies are skipped; complete earlier functions remain measurable. An internal error prints one `fn-length hook error: <type>: <message>` `systemMessage` and exits 0.

- The hook mirrors clippy exactly: same count, same threshold source, same exemptions, and it fires only where the package's Cargo lints enable `too_many_lines`, so it never blocks code clippy passes. Approved exceptions pass as clippy lets them.

- One checker (`fn_length_lib`) and one hook command string serve Claude and Codex.

- `settings.json` order: the `Edit|MultiEdit|Write` group keeps basedpyright first with the fn-length hook after it. An edit to either group keeps every entry.

- `config/showrunners.json` holds settings only and no script writes it; readers call `load_settings` or `load_settings_from(CONFIG)` on the live checkout's machine-local file. The file stays untracked (`.gitignore` names it).

- Stalled detection belongs to the stall watcher, not `unit_status.sh`. Idleness rests on the pane hash plus transcript and `subagents/` mtimes, never on status-file text.

- The follow-up claim must exist before any status record, so a refusal leaves no stale record. `assign_claude.py`'s final status write comes after every record step (`finish-pass`, `landed`); unit directors and `launch_implementation.md` read `impl_status_<slot>`.

- `prepare_session.sh` calls `unit_notifier.sh` with the session id alone to create the instance; that form stays unchanged.

- A failed token acquire exits 1 and a release fails closed when the step group cannot be inspected; callers surface that and never run an unguarded build. Count leftover processes by process group, never `pgrep -f`.

- Times in output carry their zone.

## Calibration and gotchas

- **fn-length measurement.** Against clippy on obsidian_knife, 544 of 546 functions match in name and count; the two misses sit inside a `macro_rules!` body. Hana main has 0 non-exempt functions over 100 and 43 exempt ones. Cost: +13.4 ms CPU per Rust edit over a bare launch (budget 20 ms), in-process CPU p95 9.75 ms over 1,544 files, 64.5 ms CPU on a 17,321-line file. Judge cost on CPU time (getrusage children user+sys); wall time under load is noise.

- **fn-length import cost dominates.** `tomllib` loads only when the manifest text names `lints` or a clippy config names the threshold; `dataclasses` and `pathlib` stay off the Rust path (an import test pins this). Classes in `fn_length_lib` are plain `__slots__` classes because frozen dataclasses alone broke the 20 ms budget.

- **fn-length blind spots.** Functions inside `macro_rules!` or `name! { … }` bodies are never measured (clippy sees expanded code the scan cannot expand). Edits made through shell commands (`sed`, heredocs) and growth from rustfmt reflowing a function measured at or under the limit are not seen. A failed `blocks.jsonl` append is silent and never changes the verdict. `cfg(test)` modules in package-root `examples/` targets are skipped because clippy never lints them.

- **basedpyright** exits 0 when clean. Its summary reads `0 errors, 0 warnings, 0 notes`. `scripts/hooks` is a pyright execution environment with itself on `extraPaths`, so tests import `fn_length_lib` directly.

- **Stall watch.** `~/.claude/sessions/` keeps stale `<pid>.json` records carrying a live session's id; the live record is the one whose pid answers `os.kill(pid, 0)`. Idle panes stayed byte-identical for 6.5 minutes in a live probe while a busy pane changed only from real activity. A status file reading `implementing` with no live process counts as idle.

- **Notifier launch.** A systemd oneshot (`KillMode=control-group`) kills `&!` children of its `ExecStart`; under `session-notifier` this ended `stall_watch.py` mid-send, so a nudge went out unrecorded and again the next minute. That is why run jobs get their own transient service. `launchctl submit` keeps its job alive (it re-runs every 10 s after exit 0) and launchd ends even `setsid` children when the job exits; a run that must outlive a launchd job needs its own job that removes itself.

- **Launcher races.** A peer turn can overtake a followed turn after `thread/resume`: `follow` exits 1, `impl_status` reads `error`, and the roster stays active while the peer runs, then is restored; a failure of the followed turn survives the peer turn. `board.sh release` exit 3 is benign for `verify.sh`, exit 1 is a real failure. The launcher's own end-of-worker `board.sh release … cargo` ignores failure.

- **Token-wait timing.** `verify.sh` computes `token_wait_s` as the clock after acquisition less the clock before it. The clock is bash's whole-second `SECONDS`, so a real wait under a second can read `1` and any wait can differ from real time by one second either way; `UNIT_TOKEN_CLOCK_FILE`, when set, replaces it with the integer in that file. `board.sh` waits on a held token with `sleep 3` (the acquire retry loop) and on a contended lock file with `sleep 0.05`. `test_waiting_for_peer_call_records_token_seconds` (`scripts/direct/test_verify_token_history.py`) gives each call its own clock file: a stub `sleep` first on `PATH` touches a marker when called as `sleep 3` with `TEST_WAITING` set (the second call's environment only), then blocks on a gate file instead of sleeping, so the marker proves the second call is blocked on the token. The test advances the second call's clock by 20 before releasing the token and asserts waits of exactly 0 and 20. `test_token_wait_uses_elapsed_seconds_when_no_clock_override_is_present` pins the `SECONDS` default with a Bash DEBUG trap that advances the two clock reads from 100 to 120 and asserts exactly 20. Records are matched to their calls by `-{pid}-` in the record `id`. A change to the `sleep 3` retry interval breaks the marker.

- **Pane summary.** The two-character escape pattern uses the ECMA-48 intermediate and final byte classes, never a dot that matches newlines, or a stray ESC eats the newline or tab after it. An unterminated OSC sequence discards the rest of the pane log, accepted because a worker-written summary never reaches the fallback.

- **Effort.** A Claude worker launched without `--effort` inherits `~/.claude/settings.json` (`modelSettings` → model → `effortLevel`, `xhigh` for Sonnet 5.5), so a pane line reading `thinking with xhigh effort` on a `sonnet:low` worker means the launch dropped the effort, not that the pane misleads.

- **`/unit:report`.** Reports run on request and create no notifier instance. The command accepts only the optional `tables` argument.

- **Live checks need a person.** The permission classifier refuses an automated bare `claude --bg` probe. A person must check the worker effort. Register a Claude worker as `sonnet:low`. Start it through `assign.sh --role implement`. Check that its pane shows `low effort`, not `xhigh`.

## Why

- **Block at the edit, not at the lint.** `too_many_lines` fails `verify.sh lint` and costs a repair loop; telling the agent in the same turn is cheaper than the lint round trip. The user wants no long functions, so exceptions stay rare (tests, or a stated reason in real code) and the hook honors clippy's own `allow` and `expect`.

- **Mirror clippy, check the whole file.** In a pedantic-deny crate every non-exempt function over the limit already fails the lint, so reporting all of them says nothing clippy would not; firing only where Cargo lints enable the lint keeps upstream clones such as `~/rust/bevy` quiet. Fail-open on doubt keeps a wrong hook from blocking passing code.

- **A regex tokenizer, not a parser.** The hook runs on every Rust edit beside basedpyright (one matcher group runs in parallel), so import and CPU cost are the budget; a token scan reproduces clippy's count on real code at a few milliseconds.

- **The success threshold is 75% fewer** `too_many_lines` failed steps per 100 hana clippy steps and 75% less sole-cause worker time per day, leaving room for the edits the hook cannot see (shell edits, rustfmt growth).

- **No Stop hook for asked questions** (user, 2026-10-19: "i have already asked you to get rid of the stop hook that does this"). The reply footer it once checked is gone (user, 2026-10-18).


- **Run jobs get their own service.** A background child stays in the tick's cgroup or launchd job and dies with it; a separate service keeps the run, its watchdog and its final log write alive.

- **Step groups under the token.** A killed verify used to leave its cargo build running while the token moved on, and overlapping builds are what froze the machine; recording the group before the command starts covers the first instant of a step, and release or reclaim stops only an identity-checked group, never one the holder shares with its caller.

- **Follow-up through the launcher.** The launcher is the only process that watches a worker exit, so only it can record the pass, the landed round and the board post. A `send` to a finished Codex worker holds the message for its next dispatch and leaves no pass records. One claim per worker, taken before any status write, keeps concurrent follow-ups from corrupting each other's records.

- **`implemented` after the records.** Unit directors read `impl_status_<slot>`; writing it last means a failed ledger write reads `error`, not success.

- **Effort from the registry, plain-text summaries.** A worker that inherits the global effort ignores the registry row that chose its model; pane bytes with terminal codes in a summary corrupt every reader of that file.

## Unit hosting

Unit discovery and its fixtures use herdr. Production registration starts the
run-only stall watch. Session marks are written when a pane launches; they
are not reconstructed from a terminal title or a registry label.
