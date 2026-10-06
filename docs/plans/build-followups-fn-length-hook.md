# Function-length edit hook

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A PostToolUse hook that blocks an agent the moment an edit leaves a Rust function over clippy's `too_many_lines` limit, in Claude sessions and in Codex seats through one shared checker, measured before and after against the too_many_lines failures in hana's clippy steps.

> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06 06:2x PDT: "can we create an edit hook - i am 99% sure that our hooks propagate to codex also … if a function exceeds 100 lines we provide a hook to tell the agent right there to rewrite it rather than wait for the lint? then we measure the before and after of this hook to see if we have reduced the lint warnings - i would rather fix this at the source". Every Rust workspace of the user's under `~/rust` denies clippy's `pedantic` group, which holds `too_many_lines` (limit 100); the user wants no long functions ever.

## What exists today (checked 2026-10-06 by the plan author)

- **Our Claude hooks do not reach Codex on natedev.** `~/.codex/config.toml` has no `[hooks]` and there is no `~/.codex/hooks.json`. On the Mac, `~/.codex/hooks.json` holds hand-copied entries (random-ack and basedpyright under PostToolUse, `mkdir -p /tmp/claude` under SessionStart), trusted through the Codex TUI; nothing syncs `settings.json` into it.
- **Codex hooks (rust-v0.160.1, read from source; the Mac runs 0.154.0, same behavior checked in `core/src/tools/hook_names.rs` and `hooks/src/events/post_tool_use.rs` at rust-v0.154.0):**
  - Files: the user layer reads `~/.codex/hooks.json` (Claude's `{"hooks": {"PostToolUse": [{"matcher", "hooks": [{"type": "command", "command", "timeout"}]}]}}` shape) or a `[hooks]` table in `config.toml` (`hooks/src/engine/discovery.rs`, `config/src/hook_config.rs`). `codex features list` shows `hooks stable true`.
  - Event: `PostToolUse` fires after a successful `apply_patch` with `tool_name: "apply_patch"`; matchers `Write` and `Edit` also select it (`core/src/tools/hook_names.rs:33-39`). Stdin carries `cwd` and `tool_input: {"command": "<the patch text>"}` (`core/src/tools/handlers/apply_patch.rs:437-452`). Edits made through shell commands fire `Bash`, whose input names no files.
  - Output: `{"decision": "block", "reason": "<text>"}` on stdout (or exit 2 with the text on stderr) turns the tool result into `FunctionCallError::RespondToModel(reason)`: the model reads the reason as the call's output and the patch stays applied (`core/src/tools/registry.rs:742-756`). `hookSpecificOutput.additionalContext` is recorded as model context; `systemMessage` is a UI warning; `suppressOutput` and `updatedMCPToolOutput` make the output invalid (`hooks/src/engine/output_parser.rs:207-239, 393-439`). Our banned-words hook's JSON shape parses unchanged.
  - Commands run through the session shell (`hooks/src/engine/command_runner.rs:384-417`), so `$HOME` expands; default timeout 600 s.
  - Trust: a user-layer hook runs only when `[hooks.state."<source path>:post_tool_use:<group index>:<handler index>"] trusted_hash` in `config.toml` equals its current hash, or under `bypass_hook_trust` (`discovery.rs:657-722, 772-791`; `config_rules.rs`). The hash covers event, matcher and handler config, not the file path, so editing the command string, matcher or timeout makes it `modified` and it stops running silently; editing the script it calls does not. The TUI records trust through the app-server's `hooks/list` (key, `currentHash`, `trustStatus` ∈ `managed|untrusted|trusted|modified`) and `config/batchWrite` (`keyPath: "hooks.state"`, `{key: {"trusted_hash": hash}}`, `mergeStrategy: "upsert"`, `reloadUserConfig: true`) (`tui/src/hooks_rpc.rs:57-89`).
- **Seats load user-layer hooks with no launcher change.** `codex_mesh.py` starts `codex app-server --listen ws://127.0.0.1:<port>` with no config overrides (`scripts/agents/codex_mesh.py:604`), and `thread/start` sends only `cwd`, `approvalPolicy`, `sandbox`, `model` and `serviceTier` (`codex_mesh.py:910-933`), so each thread reads `~/.codex` hooks and their trust state.
- **clippy's `too_many_lines` (rust-clippy master fcffb58, `clippy_lints/src/functions/too_many_lines.rs`; `pedantic` group; `too-many-lines-threshold` in `clippy.toml`, default 100, `clippy_config/src/conf.rs:782`):**
  - Counts only the body: the body block's source with its outer `{` `}` removed, `.trim()`ed, split by `.lines()`. A line counts when code appears outside a comment, by this loop per line: `in_comment` carries across lines; `trim_start`; empty → stop; inside a comment, find `*/`, cut after it, leave the comment, repeat; otherwise `code_in_line |= (index of "/*" or len) > 0 && (index of "//" or len) > 0`, and if `/*` comes before `//`, cut after `/*`, enter the comment, repeat. Strings are not parsed: `"a // b"` counts when code precedes it. The signature, attributes and doc comments above the body never count.
  - Lints when count > threshold (101 lines fail at the default). Closures are skipped (their lines count in the enclosing body); a nested `fn` is measured on its own and its lines also count in the outer body. Expansions of external macros are skipped.
  - Exempt when the lint level at the function is allow (`is_lint_allowed`): `#[allow(clippy::too_many_lines)]` or `#[allow(clippy::pedantic)]` on the fn or any enclosing impl, trait, mod or crate; `#[expect(...)]` lints and is fulfilled, so it emits nothing either.
  - Test code is linted: `verify.sh lint` runs `invoke_clippy --workspace --lib --bins --tests` plus one call per example (`scripts/delegate/verify.sh:931-969`). The baseline below finds 77 of 277 too_many_lines diagnostics in test or example paths.
- **Hook cost:** existing PostToolUse hooks cost 37-93 ms per call. On natedev, 2026-10-06 06:30 PDT: `lib/py -c 'import json,sys,re'` median 45.6 ms; a prototype regex tokenizer took 75.9 ms on hana's largest file (17,321 lines) and 10.7 ms on a 2,867-line file. Hana has 1,541 `.rs` files: median 251 lines, p95 2,154, p99 5,559. Claude Code runs the hooks of one matcher group in parallel, so a hook that finishes before basedpyright's adds no wall time.

## Decisions (showrunner)

- **Names:** checker library `scripts/hooks/fn_length_lib.py`; one hook entry for both agents, `scripts/hooks/post-tool-use-fn-length.py`; Codex install and trust `scripts/hooks/codex_hooks.py` (plan author, 2026-10-06).
- **Approved exceptions pass, as in clippy** (user, 2026-10-06 06:4x PDT: "when a function carries an approved exception, the hook should let it through as clippy does"). Exceptions stay rare: tests, or a specific stated reason in real code (user, 2026-10-06).
- **Mirror clippy exactly:** same count, same threshold source, same exemptions, and fire only in packages whose Cargo lints enable `too_many_lines`, so the hook never blocks code clippy would pass (an upstream clone such as `~/rust/bevy` lints nothing) (plan author).
- **Whole file:** check every function in each edited `.rs` file, not only the lines touched. In a pedantic-deny crate any non-exempt function over the limit already fails the lint, so the hook reports nothing clippy would not (plan author).
- **Block, like the banned-words hook:** `decision: block` with the reason `fn <name> at <file>:<line> is <N> lines (limit <T>): split it now`, plus that the edit was applied. The message does not mention the exemption attributes (plan author).
- **Codex delivery through the user hooks file plus a recorded trust**, written by Codex's own `config/batchWrite`; this reaches mesh seats, `codex exec` and interactive Codex alike. Ruled out: `bypass_hook_trust` in `codex_mesh`'s `thread/start` (mesh only, and interactive Codex would ask to review the untrusted hook every start); a managed hook in `/etc/codex/requirements.toml` (needs a nix rebuild on each machine for every change) (plan author).
- **Every block is logged** to `~/.local/state/fn-length-hook/blocks.jsonl` with the agent kind, so the re-measure can prove both hooks fired; an empty count needs that control (plan author).
- **Success threshold:** 75% fewer too_many_lines clippy failures per 100 hana clippy steps and 75% less sole-cause repair time per day. The hook does not see edits made through shell commands, nor growth from rustfmt reflowing a function the hook measured at or under 100; 75% leaves room for those (plan author).

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan records a too_many_lines baseline (Phase 1), makes every showrunner reply end with the current footer through a Stop hook (Phase 2), adds a PostToolUse hook that blocks Claude edits leaving a Rust function over clippy's limit (Phase 3), fixes three production status defects the showrunner routed here (Phase 4), adds a stall watcher that bumps an idle unit and tells its showrunner (Phase 5), lets a showrunner turn its footers off and on with `/showrunner:footer`, pauses dailies and footers for an adhoc review, checks that Waiting on items lead with their ETA, keeps each tmux session named after its Claude session, and leaves nothing running after a killed verify (Phase 6), extends the hook to Codex seats (Phase 7), lets the launcher carry follow-up work to a seat that is still open (Phase 8), and re-measures (Phase 9). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
- **Project started:** 2026-10-06T15:12:44.327+00:00
- **Stack:** Python 3.13, standard library only (`json`, `re`, `tomllib`, `dataclasses`, `pathlib`, `subprocess`, `shlex`, `zoneinfo`, `unittest`); `scripts/lib/py` picks a Python ≥ 3.10 on each machine (3.13 on natedev and the Mac, so `tomllib` is present). Codex CLI 0.160.1 on natedev, 0.154.0 on the Mac.
- **Layout:**
  - `scripts/production/dailies_render.py` — `--footer` also prints the Agents lines (Phase 2), judged as of the stamp (Phase 4); `test_dailies_render.py` and `test_dailies_render_agents.py` beside it
  - `scripts/whoami/run_out.py` — readings log, trailing rate, run-out lean; `test_run_out.py` beside it (Phase 4)
  - `scripts/production/unit_status.sh` — the showrunner's per-unit status; `test_unit_status.py` beside it (Phase 4)
  - `config/showrunners.json` — machine-local, ignored by `.gitignore`: quota-alert fields, `stall_minutes`, `faults_to` and the showrunners with their units (Phase 5; replaces `scripts/whoami/quota_alert.json`)
  - `scripts/production/showrunners.py` — reads and changes the config (`add`, `remove`, `import`, `list`); `test_showrunners.py` beside it (new, Phase 5)
  - `scripts/production/stall_watch.py` — the stall watcher, run by the `stall-watch` notifier instance; `test_stall_watch.py` beside it (new, Phase 5)
  - `scripts/whoami/quota_alert.py` — reads the shared config; `test_quota_alert.py` beside it (Phase 5)
  - `scripts/hooks/stop-showrunner-footer.py` — the Stop hook entry; `scripts/hooks/showrunner_footer.py` — production lookup, footer render, comparison; `scripts/hooks/test_stop_showrunner_footer.py` — their tests (new, Phase 2); the footers-off switch and its `off`, `on` and `status` command, and the ETA-first Waiting on check (Phase 6)
  - `commands/showrunner/produce.md` — the Footer section (Phase 2)
  - `commands/showrunner/footer.md` — `/showrunner:footer [on|off]` (new, Phase 6)
  - `scripts/production/tmux_names.py` — renames a tmux session to match its Claude session's name, run by the `tmux-names` notifier instance; `test_tmux_names.py` beside it (new, Phase 6)
  - `scripts/production/review_pause.py` — pauses a production's dailies and footers for an adhoc review and turns back on what it paused; `test_review_pause.py` beside it (new, Phase 6)
  - `commands/adhoc_review.md` — Steps 2 and 5 run `review_pause.py` (Phase 6; outside this unit's Owns, edited on the showrunner's decision)
  - `scripts/delegate/test_verify_token_wait.py` — the killed-holder test ends its process group (Phase 6); `scripts/delegate/verify.sh` — token reclaim (Phase 6, only when a killed holder's build outlives it)
  - `scripts/hooks/fn_length_lib.py` — the checker: lint scope, function discovery, clippy's count (new, Phase 3)
  - `scripts/hooks/post-tool-use-fn-length.py` — the hook entry for Claude payloads (Phase 3) and Codex `apply_patch` payloads (Phase 7) (new)
  - `scripts/hooks/test_fn_length.py` — its tests (new, Phase 3; extended Phase 7)
  - `scripts/hooks/codex_hooks.py` — installs and trusts the Codex hook (new, Phase 7)
  - `scripts/hooks/test_codex_hooks.py` — its tests, with a stub `codex` on `PATH` (new, Phase 7)
  - `scripts/delegate/implement.sh` — `--to <seat>` follow-up mode; `scripts/delegate/test_implement_launcher.py` beside it (Phase 8)
  - `scripts/agents/codex_mesh.py` — `follow`; `scripts/agents/test_codex_mesh.py` beside it (Phase 8); `scripts/agents/agent_bg.sh` — attach to an open Claude seat (Phase 8)
  - `commands/unit/delegate.md`, `docs/delegate/write_prompt_contract.md` — follow-ups go through the launcher (Phase 8)
  - `settings.json` — Stop hook registration (Phase 2); Claude edit hook registration (Phase 3)
  - outside the repository, at run time: `~/.local/state/fn-length-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (Phase 7); `~/.local/state/showrunner/footers-off/<slug>` and `~/.local/state/showrunner/review-paused/<slug>.json` (Phase 6)
- **Key files:**
  - `scripts/hooks/post-tool-use-banned-words.py` — the blocking convention: one JSON object with `decision: "block"`, a short `reason`, `continue: true`, a one-line `systemMessage`, and `hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext}`; read-only tools exit before heavy imports
  - `scripts/hooks/banned_words_lib.py` — the sibling-library convention the new library follows
  - `scripts/hooks/post-tool-use-basedpyright.py` — the `Edit|MultiEdit|Write` hook; `HookInput`/`ToolInput` TypedDicts for the Claude payload (`tool_input.file_path`)
  - `scripts/hooks/test_brp_launch_gate.py` — hook test convention: runs the hook as a subprocess, `setUp` with `@override` and `enterContext(tempfile.TemporaryDirectory())`, environment overrides, reads `settings.json` through `SETTINGS = HOOK.parent.parent.parent / "settings.json"`
  - `settings.json` — `hooks.PostToolUse` has a group `{"matcher": "Edit|MultiEdit|Write", "hooks": [<basedpyright>]}`; commands spell `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/<script>"`
  - `scripts/agents/codex_mesh.py:256-319` — JSON-RPC client convention (`initialize` with `clientInfo` and `capabilities: {"experimentalApi": true}`, string request ids, `_require`); read only, unchanged by this plan
  - `pyrightconfig.json` — `scripts/hooks` is an execution environment with itself on `extraPaths`, so tests import `fn_length_lib` directly
  - `~/.local/state/nightly-review/2026-10-06/work/lints/{cost.py,loop.py,clippy_fail_lints.py,tml_lengths.py}` — the measurement scripts, read-only over `~/.local/state/buildlog/index.sqlite` and its gzip step logs; outside the repository and never edited (user, 2026-10-06: "using … cost.py, loop.py and clippy_fail_lints.py")
- **Test lanes:** `scripts/hooks/`, `scripts/production/` and `scripts/whoami/` — `test_*.py` beside the scripts; this repository has no `tests/` directories.
- **Build:** none — Python and JSON; nothing compiles.
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_stop_showrunner_footer.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` (Phase 2; the first again in Phase 6); `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` and `-p 'test_codex_hooks.py'` (Phases 3 and 7); `python3 -m unittest discover -s scripts/whoami -p 'test_run_out.py'` and `-s scripts/production -p 'test_unit_status.py'` (Phase 4); `python3 -m unittest discover -s scripts/production -p 'test_showrunners.py'`, `-p 'test_stall_watch.py'` and `-s scripts/whoami -p 'test_quota_alert.py'` and `-s scripts/message -p 'test_notifier.py'` (Phase 5); `-s scripts/production -p 'test_tmux_names.py'` and `-p 'test_review_pause.py'`, with `test_showrunners.py` and `test_unit_status.py` again, and `-s scripts/delegate -p 'test_verify_token_wait.py'` (Phase 6); `python3 -m unittest discover -s scripts/delegate -p 'test_implement_launcher.py'` and `-s scripts/agents -p 'test_codex_mesh.py'` (Phase 8); run from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has, so its exit status says nothing. `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust (showrunner, 2026-10-06).
- **Invariants:**
  - The showrunner footer hook fires only in a session a running production's `showrunner-*` notifier instance targets, and fails open: any error prints one stderr line and exits 0 with no output (user, 2026-10-06).
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced brace, an unknown payload or any internal error passes the edit. An internal error prints one `systemMessage` line (`fn-length hook error: <type>: <message>`) and exits 0 (plan author).
  - One checker serves both agents; the Codex hook calls the same script with the same command string as Claude's (user, 2026-10-06: "sharing one checker script").
  - A test never writes the real `~/.codex`, `~/.local/state/fn-length-hook` or `~/.local/state/buildlog`, never runs the real `codex` or `cargo`, and never edits `settings.json` beyond the Phase 3 registration (production rules). Live checks against real files are the unit director's, in the Acceptance gates.
  - Running `codex` from a Claude session needs `dangerouslyDisableSandbox`: codex writes `~/.codex` and fails with "Operation not permitted" otherwise (`/etc/nixos/modules/common/codex.nix`).
  - Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings (user rule, `~/.claude/CLAUDE.md`).
  - `~/.claude` main is the live configuration and each merged phase goes to it at once (production rule): the Claude hook goes live when Phase 3 reaches main, the Codex hook when Phase 7's install runs on each machine.
  - Times carry their zone: this plan states PDT (America/Los_Angeles); natedev's clock and journal are EDT; buildlog stamps are UTC.

## Phases

### Phase 1 — Baseline: too_many_lines failures and seat time before the hook · status: done

#### As-built

Measured 2026-10-06 08:17 PDT over every hana clippy step since the first, 2026-10-02 09:09 PDT (span 3.96 days), by the four scripts in `~/.local/state/nightly-review/2026-10-06/work/lints/` (hashes in the re-measure's Work Order, all matching) and the read-only totals query on `~/.local/state/buildlog/index.sqlite`; every command exits 0.

- **too_many_lines failed steps: 13.3 per 100 hana clippy steps.** 1,516 steps, 473 failed; too_many_lines is the most frequent failing lint, in 201 failed steps (290 diagnostics; next is suboptimal_flops in 120), 50.7 a day.
- **Sole-cause failed steps: 4.9 per 100.** 75 failed on nothing else, 18.9 a day.
- **Sole-cause seat time: 1.07 h a day.** `loop.py` failed-call wall 1.69 h + repair-gap sum 2.57 h = 4.26 h over 3.96 days.
- `cost.py`: sole-cause lint-call wall 1.73 h (median 49 s, 5 steps without a call record), clippy step time 0.54 h; diagnostics 210 in `src` paths (some hold `#[cfg(test)]` modules), 80 in test or example paths.
- `loop.py`: 70 sole-cause failed lint calls; 64 had a next lint call within 1,800 s, gap median 117 s, p75 162 s; that next call passed 45 times, failed again 19; 4 had none.
- `tml_lengths.py`: 216 distinct flagged functions, length p25 103, median 107, p75 120, p90 140, max 180; 12 over 150, none over 200; 62 in test files by path.
- Last 24 h alone: 482 steps, 191 failed, 85 with too_many_lines (17.6 per 100), 25 sole-cause (5.2 per 100); loop 22 calls, wall 0.52 h, gap 0.90 h.
- The plan author's run (2026-10-06 06:26 PDT, span 3.89 days) gave 13.1 and 4.8 per 100 and 1.02 h a day; both rates are within 2 per 100 of it.

**Files:**
- `docs/plans/build-followups-fn-length-hook.md` — this record.

**Binds later work:** the re-measure compares against 13.3 per 100, 4.9 per 100 and 1.07 h a day; its success limits are ≤ 3.3 per 100 and ≤ 0.27 h a day.

**Gotchas:**
- `loop.py` counts sole-cause calls it later skips for lacking an end time, so its count runs 1–2 above follow-up plus none (70 vs 64 + 4; the author's run 67 vs 62 + 3); seat time uses its wall and gap sums as printed.
- A 4-day window covers every hana clippy step only until 2026-10-06 09:09 PDT; the totals query's `min(started_at)` gives the span's start.

### Phase 2 — Showrunner replies end with the current footer · status: done

#### As-built

- **Footer.** `dailies_render.py --footer --zone <IANA zone> [--next-run <HH:MM[+N]>] [--nothing-needed] [--at <YYYY-MM-DDTHH:MM[±HH:MM]>] [--outstanding <outstanding.json>]` prints the same footer the dailies report ends with (one `footer()`, fed `agent_section` on both paths): an empty line, `---`, `HH:MM <zone> update:`, an empty line, then `* ` bullets: build-hold lines (release-state lines nested as `  * ` under their hold), each Agents line in the dailies report's words, and last `* next dailies: HH:MM <zone>` (a later day adds its weekday) or `* no dailies scheduled`, with ` - nothing needed` when it applies. No `### Agents` heading. `--next-run` takes `HH:MM+N` for a run N days later; `--at` takes an offset to name one occurrence of a repeated hour.
- **Waiting on block.** Every showrunner reply ends with the footer, two empty lines, `Waiting on:`, an empty line and one or more `* ` bullets, the user's items first. The showrunner writes this block; the renderer never prints it. Waiting-on-you items (`--outstanding`) are not in the footer; an outstanding item still drops ` - nothing needed`, a deferred one only after its `after` time.
- **Stop hook.** `scripts/hooks/stop-showrunner-footer.py` imports only `json`, `os`, `sys` and `typing`, and fires only for a session that a `showrunner-*` notifier instance targets (`TARGET=session:<id>` in its `conf`, under `NOTIFIER_STATE_DIR` or `~/.local/state/notifier`) whose production doc (last token of `CHECK`) is `running`. It passes when `stop_hook_active` is set (at most one block per stop chain, so a footer that changes between attempts cannot loop the session), when `agent_id` is present, or when the session id or `last_assistant_message` is empty. The reply comes from `last_assistant_message`, never `transcript_path`: at Stop time the transcript lacks the turn's last entry. Any exception prints one stderr line, `stop-showrunner-footer: <type>: <message>`, and exits 0: the hook fails open.
- **Check.** `showrunner_footer.block_reason(instance_dirs: list[str], reply: str) -> str | None` passes a reply that ends with the footer's current text plus a valid Waiting on block, with nothing after the bullets. Otherwise the hook prints `{"decision": "block", "reason": …}`, where the reason is `REASON_HEAD`, the footer's exact current lines and a `Waiting on:` example. The expected footer comes from running `dailies_render.py --footer` with the arguments `produce.md` → Footer names: the doc's `- **User zone:**`, `--next-run` from state `NEXT_DUE` when `ENABLED=1`, `--outstanding ~/.local/state/showrunner/outstanding/<slug>.json`. It runs at most twice per block; a non-zero exit or the 10 s timeout raises `FooterError`.
- **Stamp.** `stamp(lines, now) -> tuple[datetime | None, bool]` takes the minute from the reply's last `HH:MM <zone> update:` line when it is at most 5 minutes old (`STAMP_WINDOW`), resolved by the zone abbreviation, so a footer stamped in the repeated fall-back hour (2026-11-01 01:30 PST) matches; the render passes it as `--at` with its offset. ` - nothing needed` is read from the footer's last bullet.

**Files:**
- `scripts/production/dailies_render.py` — `footer()` (the bullet footer shared by replies and reports), `footer_main` (Agents lines, `--at` offset, `--next-run HH:MM+N`), docstring and help text.
- `scripts/hooks/stop-showrunner-footer.py` — the Stop hook entry point.
- `scripts/hooks/showrunner_footer.py` — `read_production`, `render_footer`, `stamp` and `block_reason`; imported only on the showrunner path.
- `scripts/hooks/test_stop_showrunner_footer.py` — library cases, hook runs as a subprocess under a temporary `HOME`, `NOTIFIER_STATE_DIR` and `BUILD_HOLD_DIR`, never-fires and fails-open cases, and the Stop group order in `settings.json`.
- `scripts/production/test_dailies_render.py`, `test_dailies_render_agents.py`, `test_dailies_render_holds.py` — footer expectations in this layout; `--footer` Agents lines equal the report's.
- `settings.json` — the hook appended to `hooks.Stop[0].hooks`, after `stop-assistant-prose-banned-words.py`.
- `commands/showrunner/produce.md`, `commands/showrunner/dailies.md` — the footer, the Waiting on block, the turn-end rule and the hook.

**Binds later work:** `settings.json`'s Stop group holds this hook after the banned-words hook; a phase that appends to the `Edit|MultiEdit|Write` group keeps both.

**Gotchas:**
- A Stop hook runs at every turn end in every session: the non-showrunner path stays on the entry point's own imports and one `conf` read per `showrunner-*` instance (about 4 ms on a quiet machine).
- Cost readings on a loaded machine need interleaved bare and hook runs; load swamps any other delta (bare launch 22 ms quiet, 45–62 ms loaded).
- basedpyright exits 3 in every checkout (`pyrightconfig.json` names a `.venv` no checkout has), so read its output line, not its exit status.
- Replies sent before `produce.md` <StartUpdates/> step 3 registers the notifier instance are not checked.

### Phase 3 — Claude edits that leave a function over the limit are blocked at once · status: done

#### As-built

- A PostToolUse hook on Claude's `Edit`, `MultiEdit` and `Write`: after an edit to a `.rs` file in a package whose Cargo lints enable `too_many_lines` (directly or through `pedantic`), each non-exempt function over the limit is named, with the count clippy reports, in a `decision: "block"` reason with `continue: true`, a `systemMessage` and `additionalContext`; every block appends one record to `blocks.jsonl`.
- `fn_length_lib` holds `count_body_lines` (clippy's loop, quirks kept), `measure_functions` (one regex-tokenizer scan), `lint_scope` and `long_functions`. Scope: nearest `[package]` manifest, `lints.workspace` followed to `[workspace.lints.clippy]`, `too_many_lines` else `pedantic` at warn, deny or forbid; threshold from the nearest `clippy.toml` or `.clippy.toml`, else 100; any read or parse error disables.
- Exemption follows attribute attachment: `allow`, `expect` or `cfg_attr(…, allow|expect(…))` naming `clippy::too_many_lines` or `clippy::pedantic`; every outer attribute pending since the last `;`, `{` or `}` attaches to the next item, exempting a `fn` or everything inside an `impl`, `trait`, `mod` or `fn` block; `#![…]` exempts the rest of its block. A keyword inside a signature (`-> impl Fn()`) is not an item.
- Measured: against clippy on obsidian_knife, 544 of 546 functions match in name and count, the two misses inside a `macro_rules!` body in `src/yaml_frontmatter.rs`; hana main has 0 non-exempt functions over 100 and 43 exempt ones. Cost: +13.4 ms CPU per Rust edit over a bare launch (budget 20 ms); in-process CPU p95 9.75 ms over 1,544 files; 64.5 ms CPU on the 17,321-line file.
- T_claude: 2026-10-06 11:51 PDT, when the hook reached `~/.claude` main. Live smoke from a fresh Claude session at 11:52 PDT: a 101-line `Write` printed the block reason and logged an `"agent": "claude"` line.

**Files:**
- `scripts/hooks/fn_length_lib.py` — tokenizer, function scan, clippy line count, lint scope lookup.
- `scripts/hooks/post-tool-use-fn-length.py` — hook entry: payload, verdict JSON, log record.
- `scripts/hooks/test_fn_length.py` — 39 tests: counting, boundary, exemptions, discovery, scope, hook subprocess, and an import test that keeps `dataclasses`, `tomllib` and `pathlib` off the Rust path.
- `settings.json` — the handler after basedpyright in the `Edit|MultiEdit|Write` group.

**Binds later work:** `fn_length_lib.long_functions(rs_file: os.PathLike[str] | str) -> tuple[LintScope, list[FunctionLength]]` returns non-exempt functions over the threshold in source order, empty when the scope is disabled or the file is not UTF-8; `lint_scope` takes the same path type; `measure_functions(source, *, skip_example_tests=False)`. `FunctionLength(name, line, lines, exempt)` and `LintScope(enabled, threshold)` are plain `__slots__` classes. The hook command `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"` sits in the `Edit|MultiEdit|Write` group after basedpyright. Block reason: `fn <name> at <path>:<line> is <N> lines (limit <T>)` joined by `; `, then `: split it now. The edit was applied.`, with `<path>` relative to the payload's `cwd` when inside it, else absolute. Each block appends `{"at", "agent": "claude", "tool", "cwd", "file", "threshold", "functions": [{"name","line","lines"}]}` to `blocks.jsonl` under `FN_LENGTH_HOOK_STATE` when set, else `~/.local/state/fn-length-hook`. The hook takes one file per payload and logs one record today.

**Gotchas:** Functions inside a `macro_rules!` or `name! { … }` body are never measured. `cfg(test)` modules in a package-root `examples/` target are skipped, since examples are not built in test mode and clippy never lints them. A keyword is never a macro name (`if !x`), and `!=` is never a macro marker. Import cost dominates the hook: `tomllib` loads only when the manifest text names `lints` or a clippy config names the threshold, and `dataclasses` and `pathlib` stay out. A failed `blocks.jsonl` append is silent and never changes the verdict. Under load, wall time is noise; cost budgets are judged on CPU time (getrusage children user+sys).

**Ruled out:** measuring inside macro bodies, since clippy sees expanded code the scan cannot expand; frozen dataclasses, whose import cost alone broke the 20 ms budget.

### Phase 4 — Production status reads what is true: run-out after a reset, Claude through its pane, footers as of their stamp · status: done

#### As-built

- `run_out.latest_drop(readings: list[Reading], end: float) -> float | None`: the time of the latest reading at or before `end` whose used percent is below the one before it (a weekly refill or a redeemed reset), else `None`.
- `dailies_render.agent_line` starts the pace window at the latest of the computed weekly refill, `now − WINDOW` and `latest_drop(readings, checked_at)`; the week-pace fallback (no trailing rate) divides used percent by the time since that same start.
- `dailies_render.agent_section(now, zone, *, at=None)`, passed `at` by `render()` and `footer_main`: when the note's `weekly_usage_checked_at` is later than the stamped minute, each line uses the account's latest `readings.jsonl` reading at or before that minute's end, and only readings up to it for the pace; with none by then the note stands. Without `--at` nothing changes.
- Every Agents line (dailies section and footer) takes one form, then the unchanged resets part: `claude 1: 25%; runs out about Tue 22:02 PDT, before its Sun 23:00 refill; 1 reset available until Oct 22` (`22:02 PDT today` when it falls today) · `…; runs out about Mon 04:00 PDT, so it hits its Sun 23:00 refill first; …` · `…: 1%; does not run out at this pace, so it hits its Sun 23:00 refill first; …` (rate at or below 0) · `…: 100%; ran out, back at its Sun 23:00 refill; …`. Unknown usage keeps `week's usage unknown`; no line says `of the week used` or `lasts to`.
- `unit_status.sh` (`pane_claude_pid`) reads the session's pane pid with `$TM display-message -p -t "$u" '#{pane_pid}'`, walks its descendants breadth-first from one `ps -eo pid=,ppid=,args=` read in `awk`, and takes the first whose args begin `claude ` or equal `claude`; none prints `CLAUDE NOT RUNNING`. A unit whose remote-control name differs from its tmux session reads as running. The script holds no stalled-unit check.

**Files:**
- `scripts/whoami/run_out.py` — readings log, trailing rate, run-out lean, `latest_drop`.
- `scripts/whoami/test_run_out.py` — `latest_drop` cases.
- `scripts/production/dailies_render.py` — footer and Agents lines: window start, fallback, run-out wording, as of `--at`.
- `scripts/production/test_dailies_render_agents.py` — reset, as-of-stamp and wording cases.
- `scripts/hooks/test_stop_showrunner_footer.py` — the hook passes a footer whose readings changed after its stamp.
- `scripts/production/unit_status.sh` — per-unit status; Claude found through the pane.
- `scripts/production/test_unit_status.py` — pane cases, `tmux` and `ps` stubbed on `PATH`.
- `commands/showrunner/dailies.md` — the **Agents** description and example in run-out words.

**Binds later work:** the stall watcher ports `unit_status.sh`'s pane walk (`#{pane_pid}`, breadth-first over descendants, first whose args begin `claude`); stalled-unit detection belongs to the stall watcher, not `unit_status.sh`.

**Gotchas:** adding a `timedelta` to an aware datetime drops `fold`, so the end of a stamped minute is computed in absolute time, or a repeated fall-back hour admits the next hour's readings. Reset count and lean have no history and a stamp has minute resolution, so a reading or codex reset inside the stamped minute but after the render can still change a re-rendered line; one re-render clears it.

**Ruled out:** reset-count history for past stamps — the race it closes is minute-wide and about weekly; finding Claude by `pgrep -f "--remote-control $u"` — the remote-control name need not match the tmux session; a separate showrunner registry with register/remove — the `showrunner-*` notifier instances already are that set; the delegate status file as running-work evidence — it reads status text without a live process.

### Phase 5 — A stall watcher bumps an idle unit and tells its showrunner · status: done

#### As-built

- `config/showrunners.json`, machine-local and git-ignored with `config/showrunners.lock`, replaces the deleted `scripts/whoami/quota_alert.json`: `threshold_percent`, `repeat_minutes`, `stall_minutes` 5, `faults_to` natedev, `always` `["natedev"]` and `showrunners` as `[{session, zone, units}]`. A change to an absent file creates it with these defaults; a reader of an absent or invalid file prints one stderr line and exits 1.
- `showrunners.py` converts the file at the boundary into `ShowrunnerSettings`/`Showrunner` TypedDicts and runs `add <session> --zone <zone> [--unit …]`, `remove <session> [--unit …]`, `import` and `list`, each exiting 0 when the file already says what was asked. `RunningShowrunner` carries `PromptUnits(zone, unit_sessions)` or `UnreadablePrompt(reason)`; `missing_showrunners` matches sockets to find a live `showrunner-*` instance the config lacks.
- `stall_watch.py` runs each minute under the run-only notifier instance `stall-watch`. For each unit tmux session of each running configured showrunner it finds Claude under the pane; a unit whose pane hash, transcript and `subagents/` files stay unchanged for `stall_minutes` with no shell or launcher descendant (a unit waiting at a gate included) gets one bump to its own socket and one report to its showrunner per idle stretch. Before a report, transcript activity moves `since`; after it, the stretch holds until running work or a changed turn-end line; a `done:` pane is never stalled.
- A tick starts its sends together, waits at most 90 s, retries only a failed recipient with the same `--key`, reports a live unconfigured showrunner to `faults_to` once (naming the `add` command, or placeholders and the reason when its prompt is unreadable), and exits 0 at once while an earlier tick holds the non-blocking lock. `notifier.sh new <instance> --every <min> --run <cmd> [--timeout <s>]` detaches the run from the tick, writes `<instance>/run.log` (replaced each run) and logs a non-zero exit or timeout to `fire.log`.
- `quota_alert.py` sends to `always`, then each configured showrunner, each name once; `produce.md` (`<StartUpdates/>`, `<LaunchUnits/>`, `<QuotaAlert/>`, `<Wrap/>`) and `promote_unit.md` step 7 keep the list current; the live install (`add` for the four showrunners and `import` on natedev and the Mac, the `stall-watch` instance, one scratch idle unit bumped once with natedev told once) runs once the merge reaches `~/.claude` main.

**Files:**
- `.gitignore` — ignores `config/showrunners.json` and `config/showrunners.lock`.
- `scripts/production/showrunners.py` — the config owner, instance and session reading, `add`/`remove`/`import`/`list`; `test_showrunners.py` beside it.
- `scripts/production/stall_watch.py` — the watcher; `test_stall_watch.py` beside it.
- `scripts/message/notifier.sh` — run-only instances; `test_notifier.py` beside it.
- `scripts/whoami/quota_alert.py` (with `test_quota_alert.py`), `docs/quota_alerts.md`, `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md` — read and keep the list.

**Binds later work:**
- Every edit of the list, a unit or showrunner rename included, goes through `showrunners.py`'s locked write: `change()` takes an exclusive `fcntl.flock` on `config/showrunners.lock`, rereads, writes a temp file in the same directory and `os.replace`s it. Readers such as the showrunner view of `unit_status.sh` call `load_settings` on the live checkout's machine-local file.
- tmux targets are `=name` for a session and `=name:` for its pane.
- The watcher keeps one stretch file per unit name under `~/.local/state/stall-watch/` (`pane_hash`, `since`, `bump_sent`, `tell_sent`, `reported_status`) beside its tick `lock`; the files are keyed by name, so a rename moves them.
- `import` reads zone and units from the `unit_status.sh <scratch> <zone> <tmux session…>` command in each `showrunner-*` instance's prompt file; `SHOWRUNNERS_CONFIG`, `NOTIFIER_STATE_DIR` and `SHOWRUNNERS_SESSIONS` redirect the paths for tests.

**Gotchas:**
- tmux `-t name` prefix-matches, so a bare name can select a different session.
- `~/.claude/sessions/` keeps stale `<pid>.json` records carrying a live session's id; the live record is the one whose pid answers `os.kill(pid, 0)`.
- Idleness rests on the pane hash: a live probe on 2026-10-06 held idle panes byte-identical for 6.5 minutes while a busy pane changed only from real activity. A status file reading `implementing` with no live process counts as idle.

**Ruled out:** finding showrunners from `showrunner-*` timers alone (the configured list is the source, and a running unconfigured showrunner is a fault); a hook as the watcher (it runs outside every session); Claude's remote-control name as a unit's key (it can differ from the tmux session); a tracked `config/showrunners.json` (produce writes it at run time and would leave `~/.claude` dirty); status-file text as evidence of work.

### Phase 6 — Showrunner footers turn off on demand and for an adhoc review, Waiting on items lead with their ETA, tmux names follow session names, and a killed verify leaves nothing running · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 12:4x PDT, through the showrunner (natedev): "we need a way to pause outputting footers just like we can pause dailies". Shape from the showrunner: one switch per showrunner, as the dailies pause (`NOTIFIER stop UPDATES` / `start`) is; the user says "pause footers" or "resume footers" and the showrunner runs one command; while paused the Stop hook passes replies with no footer and produce stops asking for one; the Waiting on block stays; the switch holds until resumed, across compaction and resume.

The user, 13:0x PDT, through natedev: "yes - ideally if we can rename to match then any time a session is renamed by me - something should notice and rename the tmux"; placed here by the showrunner as its own job, since it covers every Claude session in tmux, not only units.

The user, 13:1x PDT, through natedev: "it should be a showrunner skill /showrunner:footer off and /showrunner:footer on - default is on".

The user, 13:4x PDT, through natedev: "for a /showrunner:produce skill - anytime the user does an adhoc_review it should automatically pause both the current dailies and the current footers until the adhoc review is finished at which point it should ask if the user wants to turn dailies and footers back on". Placed here by the showrunner because it uses the footer switch; the showrunner decided this unit edits `commands/adhoc_review.md`, which is outside its Owns.

The user set a new Waiting on format, which `commands/showrunner/produce.md` holds on `main` since 1796c50: the user's items first; every other item leads with its ETA, without the zone, soonest first (`19:45 (18:20–23:55) - startup Phase 16`); items with no ETA last, led by `no ETA measured - `. The showrunner (13:4x PDT) asked the footer hook to enforce it.

The showrunner, 13:3x PDT: `scripts/delegate/test_verify_token_wait.py::test_killed_verify_holder_is_reclaimed_promptly` (this unit's a290934) leaves a process tree behind on every run — `verify.sh`, a subshell, the stub cargo and two `tee`s, orphaned to `systemd --user` and forking `sleep 0.1` ten times a second — because `first.kill()` ends only the top `bash` and `tearDown` deletes the directory before the stub reads `release`. It also asked whether a real holder killed the same way leaves its cargo running beside the next holder's. Placed here by this unit: its own lane in this phase, so the Codex phase stays Phase 7 for G3.

**Goal:** a renamed Claude session's tmux session takes the new name within a minute, with the showrunner config following; after `/showrunner:footer off` a showrunner ends each reply with the Waiting on block alone and the footer hook passes it, until `/showrunner:footer on`; footers are on by default; an adhoc review in a showrunner's session pauses its dailies and footers and, at its end, asks whether to turn back on what it paused; the footer hook blocks a Waiting on block whose items break the ETA-first format; and a killed `verify.sh` leaves no process of its own running, in the test and on the real path.

**Spec:**
- **The switch** is one file per production, `~/.local/state/showrunner/footers-off/<slug>` (present means footers off; absent, the default, means on), keyed like `outstanding/<slug>.json`, so it outlives compaction, `claude --resume` and a new `showrunner-<slug>` instance target. `SHOWRUNNER_STATE_DIR` overrides `~/.local/state/showrunner` for tests.
- **The skill:** `commands/showrunner/footer.md` is `/showrunner:footer [on|off]` (`argument-hint: "[on|off]"`), run in the showrunner's session as `/showrunner:interval` is. `off` and `on` run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/showrunner_footer.py" off|on` and report its output; with no argument it runs `… status` and changes nothing; any other argument says so and stops. **The script** finds the session's productions as the hook does (every `showrunner-*` instance whose conf has `TARGET=session:$CLAUDE_CODE_SESSION_ID`) and sets (`off`) or clears (`on`) the switch for each; `status` prints `<slug> footers off` or `<slug> footers on`, one line per production. With no such instance it exits 1 with `no production targets this session`. `off` and `on` exit 0 when the switch already says what was asked.
- **The hook while footers are off:** a production whose switch is set needs no footer. The hook still checks the Waiting on block (two empty lines, `Waiting on:`, an empty line, `* ` bullets) and blocks a reply that lacks it, with a reason that shows only the Waiting on shape and says footers are off (`/showrunner:footer on` turns them back on). A reply that still carries a footer passes while they are off. The fast path stays: a session with no `showrunner-*` instance imports nothing more than today.
- **Tmux names follow session names.** `scripts/production/tmux_names.py`, run by a second run-only notifier instance `tmux-names` (`notifier.sh new tmux-names --every 1 --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/tmux_names.py"`), each minute reads every live session's `name` from `~/.claude/sessions/<pid>.json` and finds its tmux session through `TMUX_PANE` in that process's environment (`/proc/<pid>/environ`; where no tmux server runs, as on the Mac today, it exits 0 doing nothing). When the names differ it runs `tmux rename-session -t =<old> <new>` and, in the same step, renames any unit entry holding `<old>` in `config/showrunners.json` through `showrunners.py`'s locked write (the `rename <old> <new>` command below). It skips and reports to `faults_to`, once per case, a tmux session that hosts more than one Claude and a new name already taken by another tmux session. Every unit-name reader takes the names from `config/showrunners.json`: `unit_status.sh` reads a showrunner's units from it (`unit_status.sh <scratch> <zone> --showrunner <session>`), and produce's prompt line in `<StartUpdates/>` uses that form, so a rename cannot leave a stale list.
- **The Waiting on format.** The hook's Waiting on check, with footers on or off, also reads each bullet. A bullet is an ETA item when it leads with `<lead> - `, where `<lead>` is `[<Day> ]HH:MM[+1][ (HH:MM[+1]–HH:MM[+1])]` and `<Day>` a three-letter English weekday; `+1` or a weekday marks a time past midnight. A bullet led by `no ETA measured - ` is a no-ETA item; any other bullet is the user's. The hook blocks a reply when:
  - a bullet that does not lead with a time carries its ETA mid-line: `ETA` followed by a time, or an `HH:MM (HH:MM–HH:MM)` range, anywhere after its start;
  - a leading time is followed by a zone, an all-capitals word of two to five letters such as `PDT` or `UTC`;
  - the leading times are out of order, compared as (day, minutes): an unmarked time is today, `+1` is tomorrow, and a weekday is the next such day counted from today in the production's zone, today's own weekday being today;
  - the kinds are out of order: the user's items, then ETA items, then no-ETA items.
  The reason names the broken rule, states the format and shows the user's example `19:45 (18:20–23:55) - startup Phase 16` and a `no ETA measured - <item>` line.
- **An adhoc review pauses dailies and footers.** `scripts/production/review_pause.py`, run as `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/review_pause.py" <command>`, finds the session's productions as `showrunner_footer.py` does and acts on each:
  - `pause`: when `NOTIFIER status showrunner-<slug>` reports it enabled, runs `NOTIFIER stop showrunner-<slug>`; when footers are on, sets the footers-off switch. It records what it changed in `~/.local/state/showrunner/review-paused/<slug>.json` (`{"dailies": true|false, "footers": true|false}`), written atomically, so the record outlives compaction and a resumed session. When a record already exists it changes nothing and keeps it. It prints one line per production — `<slug>: paused dailies and footers`, `<slug>: paused footers; dailies were already off`, `<slug>: nothing to pause` — and nothing, exit 0, when no production targets the session.
  - `resume both|dailies|footers|none`: turns back on only what the record says the review paused and the answer names (`NOTIFIER start showrunner-<slug>`; clears the switch), deletes the record, and prints what it turned on.
  - `status`: one line per record, `<slug>: review paused dailies, footers`, or nothing.
  - `commands/adhoc_review.md` Step 2 runs `pause` before the first item and shows any line it printed. Step 5 runs `status`; when a record exists, it asks the user one line naming only what the review paused — `Turn dailies and footers back on? (yes / dailies only / footers only / leave off)`, or `Turn footers back on? (yes / leave off)` when only footers were paused, and the same for dailies — then runs `resume` with the answer (`yes` is `both`, `leave off` is `none`; the record goes either way). Both steps say the record outlives compaction, so a review resumed after one ends the same way. A session no production targets sees none of this.
- **A killed verify leaves nothing running.** `test_killed_verify_holder_is_reclaimed_promptly` starts the first `verify.sh` with `start_new_session=True`, ends its group with `os.killpg` in `finally` and in `tearDown` for any group still started, and the teardown asserts no process of that group remains. The real path: in a scratch session directory, SIGKILL a holder `verify.sh` mid-step and read whether its cargo outlives it while the next caller reclaims the token. When it does, reclaim also ends what the dead holder started and left running — its recorded step processes and their descendants, never a process group it shares with its caller — and a test pins that the reclaiming caller's build runs alone. The checkpoint notice says which was found.
- **Several productions in one session.** The reply must satisfy every production that targets the session, each by its own switch: a footer for each production whose footers are on, and the Waiting on block always. The checks take the switch as a named state (footers on, footers off), never a bare boolean or `None`; where the new checks read `Production.next_due` or `FooterComparison.stamped`, their `None` is converted at that boundary into a named state (a paused or unscheduled notifier; a reply with no stamp).
- **Renames reach every name holder.** `rename-unit <old> <new>` becomes `rename <old> <new>` in `showrunners.py`: under the same lock it renames a unit entry and a showrunner's `session` alike, so a renamed showrunner keeps its units and is not reported missing. In the same step it moves the stall watcher's state for that name (`stall_watch.py` gains the function that renames a unit's or showrunner's stretch files under `~/.local/state/stall-watch/`), so a stretch already reported is not bumped again under the new name.
- **Import after the prompt change.** `showrunners.py import` reads both prompt forms: the old `unit_status.sh <scratch> <zone> <tmux session…>` gives zone and units; the new `unit_status.sh <scratch> <zone> --showrunner <session>` gives the zone only and adds no units (the config already holds them), never `--showrunner` or the session as unit names. `commands/showrunner/promote_unit.md` step 7 stops editing the prompt's unit list; `showrunners.py add` and `remove` are the only unit changes.
- **The killed holder's leftovers end before the token moves.** When the real path leaks, the cleanup lives where reclaim happens, `scripts/delegate/board.sh acquire`: it ends the dead holder's leftover processes (from an ownership record the holder writes at acquire — its step processes, never a process group it shares with its caller) and only then takes the token. A test pins that the prior build has ended before the next build starts.
- **Produce:** the Footer section in `commands/showrunner/produce.md` gains: `/showrunner:footer off` and `/showrunner:footer on` set the switch (the user types them, or says "footers off" / "footers on" and the showrunner runs them); while `/showrunner:footer` reports off, end each reply with the Waiting on block alone; run `/showrunner:footer` after every compaction and resume, since the switch outlives both; an adhoc review in this session pauses the dailies and footers and asks at its end (`commands/adhoc_review.md` Steps 2 and 5). The Waiting on block's rule names the hook's check. `<StartUpdates/>` changes its `unit_status.sh` line to the `--showrunner` form and, beside the `stall-watch` step, creates the `tmux-names` instance when `NOTIFIER status tmux-names` reports none; the dailies keep their own switch.

**Files:**
- `commands/showrunner/footer.md` — new: `/showrunner:footer`.
- `scripts/hooks/showrunner_footer.py` — the switch, the `off`, `on` and `status` command, and the check while footers are off.
- `scripts/hooks/stop-showrunner-footer.py` — passes the switch state to the check.
- `scripts/hooks/test_stop_showrunner_footer.py` — footers-off cases.
- `commands/showrunner/produce.md` — the Footer section; the `unit_status.sh` line in `<StartUpdates/>`.
- `scripts/production/tmux_names.py` — new; `scripts/production/test_tmux_names.py` — new.
- `scripts/production/showrunners.py` — `rename`, `import` of both prompt forms; `scripts/production/test_showrunners.py`.
- `scripts/production/unit_status.sh` — `--showrunner`; `scripts/production/test_unit_status.py`.
- `scripts/production/review_pause.py` — new; `scripts/production/test_review_pause.py` — new.
- `commands/adhoc_review.md` — Steps 2 and 5; also touches (outside Owns, the showrunner's decision).
- `commands/showrunner/promote_unit.md` — step 7 leaves the prompt's unit list alone.
- `scripts/production/stall_watch.py` — renames a name's stretch files; `scripts/production/test_stall_watch.py`.
- `scripts/delegate/board.sh` — reclaim ends the dead holder's leftovers first, only when the real path leaves a build running.
- `scripts/delegate/test_verify_token_wait.py`; `scripts/delegate/verify.sh` — token reclaim, only when the real path leaves a build running.

**Seats:** 2 writers, each writing its lane's tests from the Spec before its code.
- `impl` — footers: `commands/showrunner/footer.md`, `scripts/hooks/showrunner_footer.py`, `scripts/hooks/stop-showrunner-footer.py`, `commands/showrunner/produce.md` (the Footer section, the Waiting on rule and the `<StartUpdates/>` `unit_status.sh` line), `scripts/production/review_pause.py`, `commands/adhoc_review.md`, `commands/showrunner/promote_unit.md`, and their tests `scripts/hooks/test_stop_showrunner_footer.py` and `scripts/production/test_review_pause.py`, with a temporary `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR`, production doc and a stub notifier:
  - `off` then a reply with only a valid Waiting on block passes; the same reply with no Waiting on block is blocked with the footers-off reason; `on` then the same reply is blocked with the footer reason, as today;
  - the switch outlives a new instance for the same slug targeting another session id;
  - `status` prints one line per production of the session; `off` twice exits 0; a session no instance targets exits 1 with the message;
  - footers off in one production leave another production's footers on in another session; with no switch file `status` says on; one session targeted by two productions, one off and one on, needs the footer of the one that is on;
  - Waiting on: the user's example passes; each blocking case is blocked with its rule named — an ETA mid-line, a zoned leading time, `21:00` before `19:45`, a user item after an ETA item, a no-ETA item before an ETA item; `23:30` then `00:15+1` passes and `23:30` then `00:15` is blocked; a weekday after the evening passes;
  - review pause: `pause` stops enabled dailies and sets the switch, recording both; with dailies already stopped it records only footers and `resume both` leaves dailies stopped; a second `pause` keeps the first record; `resume footers` turns footers on and leaves dailies off; `resume none` changes nothing and deletes the record; no production prints nothing.
- `test` — opens as a writer: tmux names (`scripts/production/tmux_names.py`, `scripts/production/showrunners.py` `rename` and `import`, `scripts/production/stall_watch.py` and `scripts/production/test_stall_watch.py` for the state rename, `scripts/production/unit_status.sh` `--showrunner`, with `scripts/production/test_tmux_names.py`, `scripts/production/test_showrunners.py`, `scripts/production/test_unit_status.py`, stubbing `tmux` with a temporary sessions directory, environ files and config) and the killed verify (`scripts/delegate/test_verify_token_wait.py`, `scripts/delegate/verify.sh` and `scripts/delegate/board.sh` when needed):
  - tmux names: a renamed session's tmux session is renamed and its unit entry follows in one tick; a renamed showrunner's `session` follows and it is not reported missing; a reported stretch is not bumped again after its unit is renamed; `import` reads the `--showrunner` prompt form as zone only; equal names do nothing; two Claudes in one tmux session and a taken name are skipped and reported once; no tmux server exits 0; `unit_status.sh --showrunner` reads the units from the config;
  - killed verify: the killed holder's group is gone at teardown; when the real path leaked, the reclaiming caller's run finds no build of the dead holder's still running, and the prior build ended before the next started.

**Constraints from prior phases:**
- Phase 2 (as built): the hook fails open (one stderr line, exit 0, no output) and passes a reply after any Stop-hook block; keep both. On a loaded machine a hook's cost is a delta between two medians that load swamps; measure bare and hook runs interleaved.
- Phase 4 (as built): the footer is judged as of its stamp (`--at`); unchanged here.
- Phase 5 edits `produce.md` (`<StartUpdates/>`, `<LaunchUnits/>`, `<QuotaAlert/>`, `<Wrap/>`); this phase edits its Footer section, the Waiting on rule and the `<StartUpdates/>` `unit_status.sh` line.
- Before dispatch, merge the commit holding the new Waiting on rule (1796c50, on `main`) into this branch, so the seats read it.

**Acceptance gate:**
- From the worktree root: `python3 -m unittest discover -s scripts/hooks -p 'test_stop_showrunner_footer.py'`, `python3 -m unittest discover -s scripts/production -p 'test_tmux_names.py'`, `-p 'test_showrunners.py'`, `-p 'test_unit_status.py'` and `-p 'test_review_pause.py'`, and `python3 -m unittest discover -s scripts/delegate -p 'test_verify_token_wait.py'` green; `bash -n scripts/delegate/verify.sh` when it changed; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- After one run of `test_verify_token_wait.py`, no process it started remains (read by process group, never `pgrep -f`).
- Live (natedev, once the merge reaches `~/.claude` main): `/showrunner:footer off`, one reply ending with the Waiting on block alone passes, `/showrunner:footer` says off after a `/compact`, `/showrunner:footer on`, and the next reply without a footer is blocked; a reply whose Waiting on item carries `ETA 19:45 PDT` mid-line is blocked; `/adhoc_review` in natedev's session stops its dailies and footers, and its wrap-up asks and turns back on what the user picks; `notifier.sh status tmux-names` shows the instance after a produce resume.

### Phase 7 — Codex seats get the same block after `apply_patch` · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 06:2x PDT: "i am 99% sure that our hooks propagate to codex also". They do not on natedev (see What exists today).

**Goal:** a Codex seat whose `apply_patch` leaves a function over the limit reads the same reason as its tool output, on natedev and the Mac, through the Phase 3 script and command string.

**Spec:**
- `post-tool-use-fn-length.py` accepts `tool_name: "apply_patch"`: the files are the patch's `*** Add File: <path>` and `*** Update File: <path>` headers, an `*** Update File:` followed by `*** Move to: <new>` checking `<new>`; `*** Delete File:` is skipped. Relative paths resolve against the payload's `cwd`. The `.rs` filter, verdict, output and log are Phase 3's, with `"agent": "codex"`. The reason is the whole message Codex shows the model, so it keeps `The edit was applied.` A patch may touch several `.rs` files: check each, print one block whose reason names every function over the limit across them, and append one `blocks.jsonl` record per affected file. Payload parsing returns a semantic type (`AppliedRustEdits` with the cwd and files, or `IgnoredEdit`), never empty strings or a bare optional path.
- `codex_hooks.py` (typed, standard library; `CODEX_BIN` when set, else `codex` on `PATH`, else `~/.local/bin/codex`; `CODEX_HOME` when set, else `~/.codex`):
  - The one hook: event `PostToolUse`, matcher `apply_patch`, handler `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py\"", "timeout": 10}`. The command string is byte-identical to the Phase 3 `settings.json` entry, so the trust hash is the same on both machines.
  - `install`: read `<CODEX_HOME>/hooks.json` (absent → `{"hooks": {}}`; invalid JSON → exit 1 naming the file, which stays untouched). If no `PostToolUse` group with matcher `apply_patch` holds a handler equal to this one (command and timeout), append a new group `{"matcher": "apply_patch", "hooks": [<handler>]}` at the end of `hooks.PostToolUse`, so existing groups keep their indexes and their recorded trust (the Mac's file has PostToolUse groups 0 and 1, trusted); write with 2-space indent to a temp file in the same directory, then `os.replace`. Then trust it: start `codex app-server` (stdio, the default transport) with cwd `$HOME`; `initialize` as `codex_mesh.py` does (`clientInfo.name` `"codex_hooks"`); `hooks/list` with `{"cwds": [<HOME>]}`; take the entry whose event is `PostToolUse`, matcher `apply_patch`, handler command and timeout equal this one's, and whose `sourcePath` is `<CODEX_HOME>/hooks.json`; if `trustStatus` is `untrusted` or `modified`, `config/batchWrite` with `{"edits": [{"keyPath": "hooks.state", "value": {<key>: {"trusted_hash": <currentHash>}}, "mergeStrategy": "upsert"}], "reloadUserConfig": true}`; list again. Exit 0 printing `trusted <key>` only when the entry reads `trusted` and `enabled: true`; otherwise exit 1 naming the status, or the RPC error (a `config/batchWrite` refusal reports the server's message, which names any invalid config key). The whole exchange is bounded at 30 s and the server is killed on exit.
  - `check`: the same list without writing; exit 0 when trusted and enabled, else exit 1 with `fn-length codex hook: <absent|untrusted|modified|disabled>`.
- `codex_mesh.py` is unchanged: seats read user-layer hooks with no override.

**Files:**
- `scripts/hooks/post-tool-use-fn-length.py` — `apply_patch` payloads.
- `scripts/hooks/codex_hooks.py` — new: `install`, `check`.
- `scripts/hooks/test_fn_length.py` — `apply_patch` cases.
- `scripts/hooks/test_codex_hooks.py` — new: tests with a stub app-server.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/codex_hooks.py`; post `done` without waiting for the test seat.
- `test` — `scripts/hooks/test_fn_length.py` (the `apply_patch` cases) and `scripts/hooks/test_codex_hooks.py`, from the Spec alone; owns the final suite run:
  - `apply_patch` payloads: an Update File over the limit blocks with `"agent": "codex"`; an Add File relative to `cwd`; Update plus Move to checks the new path; Delete File only prints nothing; a patch touching only `.md` prints nothing; a patch updating two `.rs` files over the limit prints one block naming both and appends two log records;
  - `codex_hooks.py` against a stub `codex` executable placed alone on `PATH` that answers stdio JSON-RPC from a state file (initialize, `hooks/list` built from the temp `hooks.json` and recorded trust, `config/batchWrite` recording the edit): a fresh install creates `hooks.json` with one group and trusts it; a two-group file shaped like the Mac's gains the group at index 2 with groups 0 and 1 unchanged; a second install adds no group and sends no `config/batchWrite`; a copy of the handler under another matcher does not count as installed; a `modified` entry is trusted again; `check` exits 0 and 1 by status; a stub that exits at once makes `install` exit 1 with a message; invalid `hooks.json` exits 1 and the file is byte-identical after.

**Constraints from prior phases:**
- Phase 2 (as built): on a loaded machine a hook's cost is a delta between two medians that load swamps (load avg 117 measured bare launches at 45–62 ms against 22 ms quiet); measure any cost budget with bare and hook runs interleaved, one of each per iteration.
- Phase 3 built `fn_length_lib.long_functions(rs_file: os.PathLike[str] | str) -> tuple[LintScope, list[FunctionLength]]` (plain `__slots__` classes, no dataclasses; `measure_functions(source, *, skip_example_tests=False)`), a hook entry that takes one file per payload and logs one record, its block JSON, the log at `FN_LENGTH_HOOK_STATE` or `~/.local/state/fn-length-hook/blocks.jsonl`, and the `settings.json` command string this phase repeats byte for byte.
- Tests never run the real `codex`, never write `~/.codex`, and set `CODEX_HOME`, `HOME` and `FN_LENGTH_HOOK_STATE` to temporary directories.

**Acceptance gate:**
- From the worktree root, both `unittest discover` lines from the Delegation Context green; basedpyright 0 errors and 0 warnings on every changed `.py` file.
- Install (unit director, after the merge reaches `~/.claude` main and the Mac has pulled): `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/codex_hooks.py" install`, then `check`, on natedev (`dangerouslyDisableSandbox`), and on the Mac over `ssh mac` printing `rc=$?` inside the command (Mac ssh always exits 0). Both print `trusted …`; the Mac's existing PostToolUse groups 0 and 1 and its SessionStart entry are unchanged.
- End-to-end smoke (natedev, then the Mac over `ssh mac`; the two run different Codex versions): in a scratchpad crate denying `pedantic`, `codex exec --skip-git-repo-check -C <crate> "<ask for one function of 101 statement lines added with apply_patch>"` (`dangerouslyDisableSandbox`); the run's output shows the reason and `blocks.jsonl` gains an `"agent": "codex"` line, on each machine. Delete the crate. T_codex is the time both smokes have passed.

### Phase 8 — The launcher carries follow-up work to a seat that is still open · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 13:1x PDT, through the showrunner (natedev): "the launcher (implement.sh) carries follow-up work to a seat that is still open. The unit director gives it the open seat and a message. The launcher sends it, records the pass (and the repair round's landed state, when it resolves one), waits for the seat's done, posts done, and exits, which wakes the unit director, just as it does for a new seat. Messages without the launcher stay for questions only." Placed after the Codex phase by the showrunner, so G3 stays at Phase 7. Measured: on 2026-10-06 this unit redirected an open Codex seat twice by message (Phase 5), and neither message recorded a pass or woke the unit director.

**Goal:** one `implement.sh --to <seat>` call sends follow-up work to a seat still open in the phase. The run records it as that slot's pass and resolves the repair round. The launcher's exit wakes the unit director, just as a new seat's does, for Codex and Claude seats alike.

**Spec:**
- **`implement.sh --to <seat>`** takes today's positional arguments, with the prompt file as the message. It skips `seat_name.sh` and addresses `<seat>`, the full name from `mesh_roster.json` (Codex) or the `seats` ledger (Claude), whichever holds it. Before any record, it exits 2 with a message when the seat is in neither, is `mesh=none`, or is busy: roster `running` or `waiting_capacity`, its slot's `impl_status_<slot>` reads `implementing`, or `claude agents` reads it busy or `gone`.
- Otherwise it runs today's path unchanged:
  - it truncates `impl_summary_<slot>.txt` and writes `impl_status_<slot>` (`implementing`, then `implemented` or `error`);
  - it calls `start-pass` and `finish-pass` as the launcher;
  - under `PLAN_DELEGATE_RESOLVES_ROUND=1`, it runs `findings.py landed` or `abandon --edits-landed`;
  - it posts a register line carrying `role=<kind>` and `follow-up to <seat>`, and the `launcher:` `done` or `blocked` post.

  It appends a fixed trailer to the message: write `impl_summary_<slot>.txt` as your last act, then post `done`.
- **Codex:** a new `codex_mesh.py follow --session-dir <dir> --to <seat> --message-file <path>`.
  - It refuses unless the roster entry reads `done` or `failed`.
  - It sets the entry to `running`, calls `thread/resume`, starts one turn on that thread, and streams it. The streaming loop is the one `start` uses, pulled out of `_attach_and_run` into one function both verbs call. It ends with the same roster states.
  - It waits on the turn id it started, so an earlier `done` cannot end the wait.
  - When the recorded app-server is gone, it starts one and resumes the thread from its rollout. A resume that fails exits 1 with the server's error.
- **Claude:** it sends through `send.py` (a `QUEUED` result is an error), then waits with a turn-completion check that cannot miss a turn shorter than the 15 s poll: a transcript turn count, not busy-then-idle.
- **Docs:**
  - `docs/delegate/write_prompt_contract.md` <PhaseMesh/>: a finished Codex seat is reached only through `implement.sh --to`.
  - `commands/unit/delegate.md` <FixDispatch/>: a repair whose files sit with an open seat may go to that seat through `implement.sh --to`.
  - Both docs: a message to a seat without the launcher is for questions only.
  - <DispatchContract/> item 6 and <FixDispatch/>'s third outcome name the follow-up as well as a new seat.

- **One claim per seat.** `implement.sh --to` takes the seat in one locked step that checks the full seat name, slot and family and marks it busy before any status or pass write; a second follow-up to the same seat, started at the same moment, exits 2 with nothing recorded. `codex_mesh.py`'s roster record gives the seat a named state (followable: done or failed; active, with its turn id) instead of a free-form `status` string and an empty `turn_id`.
- **When a Claude seat's turn is done.** The launcher reads the seat's transcript (`~/.claude/projects/*/<session id>.jsonl`, the id from `~/.claude/sessions/<pid>.json`), counts its finished assistant turns before sending, and waits until the count grows and the seat is idle; `scripts/agents/agent_bg.sh` gains that attach path beside its launch path. A turn shorter than the poll interval still counts.

**Files:**
- `scripts/delegate/implement.sh` — `--to`.
- `scripts/agents/codex_mesh.py` — `follow` and the shared turn loop.
- `scripts/agents/agent_bg.sh` — attach to an open Claude seat.
- `commands/unit/delegate.md`, `docs/delegate/write_prompt_contract.md` — the rules above.
- `scripts/delegate/test_implement_launcher.py` — `--to` cases (copy `findings.py` into its temporary tree so round resolution is covered).
- `scripts/agents/test_codex_mesh.py` — `follow` cases on `StubAppServer`.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/delegate/implement.sh`, `scripts/agents/codex_mesh.py`, `scripts/agents/agent_bg.sh`, `commands/unit/delegate.md`, `docs/delegate/write_prompt_contract.md`; post `done` without waiting for the test seat.
- `test` — `scripts/delegate/test_implement_launcher.py`, `scripts/agents/test_codex_mesh.py`, from the Spec alone; owns the final suite run:
  - `follow` on a `done` seat runs one turn on the same thread (`thread/resume`, then one `turn/start`), and the roster ends `done`;
  - `follow` on a `running` seat exits 2 and sends nothing;
  - an earlier turn's completion does not end `follow`'s wait;
  - `--to` records one pass for the slot and resolves the round `landed`; a worker error records `abandon --edits-landed` and the `blocked` post;
  - an unknown seat, or a busy one, exits 2 before any pass or status write.

**Constraints from prior phases:**
- Phase 5 (as built): the stall watcher counts `implement.sh` as running work, so a follow-up launcher keeps its unit from being bumped.
- `commands/unit/delegate.md` and `docs/delegate/write_prompt_contract.md` are read by every unit director; name them in the checkpoint notice as `also touches` per production_format item 9.
- Tests never start a real `codex` or `claude`, never write `~/.claude/sessions`, and run in temporary session directories.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/delegate -p 'test_implement_launcher.py'` and `python3 -m unittest discover -s scripts/agents -p 'test_codex_mesh.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files; `bash -n` on the changed shell scripts.
- Live (unit director, after the merge reaches `~/.claude` main): in a scratch session directory and scratch worktree, a one-seat Codex dispatch writes a file. Then `implement.sh --to` that seat asks for a second line. The pass shows in the progress table, `impl_status` reads `implemented`, and the launcher's exit wakes the unit director. Delete the scratch worktree after.

### Phase 9 — Re-measure four days after both hooks are live · status: todo

#### Work Order

**Starts:** 96 hours after T_codex (Phase 7's As-built) — a wait on the clock, not a production gate. The window must hold no hours from before T_codex.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Goal:** the plan states whether the hook cut too_many_lines failures at the source, against Phase 1's baseline and a stated threshold, with proof that both hooks fired.

**Spec:**
- Confirm the four script hashes: `sha256sum` in `~/.local/state/nightly-review/2026-10-06/work/lints/` gives `c2de20870ab2de9b5438e469c565d25c48185438ab42dd77807d817655b5d4fa cost.py`, `a775765b96311a783c8d6af8cdc925071b89b4ac423ae8ffa75b88fe81bf9abf loop.py`, `e178642d9bb1690562c132d824256198cc8f6e622b779b94ca76abbc75e841ce clippy_fail_lints.py`, `18754c36c8c545928a082e3bded49c688f029221b917b0f1e6c164028e4b6522 tml_lengths.py`; a mismatch stops the phase. Run, in the background with `set -o pipefail`, from `~/.local/state/nightly-review/2026-10-06/work/lints/`: `python3 cost.py 4 too_many_lines`, `python3 loop.py 4 too_many_lines`, `python3 clippy_fail_lints.py 4`, `python3 tml_lengths.py 4` (only when `clippy_fail_lints.py` reports at least one too_many_lines failure; it indexes an empty result, so with none record zero lengths instead), and the totals query `select count(*), sum(status!=0), min(started_at) from steps where repo='hana' and step='clippy' and started_at>=datetime('now','-4 days')` against `~/.local/state/buildlog/index.sqlite` opened read-only (`?mode=ro`), between T_codex + 96 h and T_codex + 100 h, so the 4-day window starts after T_codex.
- Derive the same three numbers as Phase 1: too_many_lines-including failed steps (`clippy_fail_lints.py`) per 100 hana clippy steps; sole-cause failed steps (`cost.py`) per 100; sole-cause seat time per day = (`loop.py` failed-call wall + repair-gap sum) / span days, the span running from the window's first hana clippy step to T_codex + 96 h. The window is fixed at T_codex to T_codex + 96 h and steps after it are left out, so a run that starts late, after Phase 8, gives the same numbers.
- **Success** when all three hold: too_many_lines-including failed steps per 100 hana clippy steps ≤ 25% of Phase 1's rate (13.3 per 100, so ≤ 3.3); sole-cause seat time per day ≤ 25% of Phase 1's (1.07 h, so ≤ 0.27 h); and the control — `blocks.jsonl` holds at least one `"agent": "claude"` and one `"agent": "codex"` line inside the window, counting natedev's file and the Mac's read over `ssh mac`. Earlier smokes may fall outside the window, so before the run the unit director makes one 101-line edit through each agent inside it (a Claude `Write`, a Codex `apply_patch`) and confirms both the visible block and its log line. A failed control means a hook did not fire, and the rates say nothing about the hook; a block with no log line leaves the control unproven, since a failed log append is silent.
- Residuals: for each too_many_lines diagnostic in the window's failed steps (file and line from the step log), read the step log and the buildlog row read-only and match it to a `blocks.jsonl` record with the same worktree (`cwd`), file and function name, earlier than the step's start. Matched: the hook fired and the agent linted before splitting. Unmatched: an edit the hook did not see (a shell edit, rustfmt growth past the limit, or a person), a function inside a `macro_rules!` body (never measured), or `unknown` when the logs cannot say. Report each count and the five most frequent files.
- Write the numbers, the verdict and the residual counts into this phase's As-built, and send the showrunner the verdict line.

**Files:**
- `docs/plans/build-followups-fn-length-hook.md` — this phase's As-built (the closeout writes it).

**Seats:** 1 writer — `impl` runs the commands and reports; nothing splits and there is no code or test lane.

**Constraints from prior phases:**
- Phase 1's As-built holds the baseline: 13.3 too_many_lines failed steps per 100 hana clippy steps, 4.9 sole-cause per 100, 1.07 h sole-cause seat time a day (run 2026-10-06 08:17 PDT, span 3.96 days). Phase 3's As-built holds T_claude, Phase 7's T_codex.
- Phase 3 (as built): functions inside a `macro_rules!` or `name! { … }` body are never measured, and `cfg(test)` modules in a package-root `examples/` target are skipped (examples are not built in test mode); a failed `blocks.jsonl` append is silent.
- The scripts and the buildlog are read-only (user, 2026-10-06).
- Saved run output stays under a few GB: read each run and delete it before the next.

**Acceptance gate:**
- All hashes match, every command exits 0, and the As-built states each of the three success conditions as met or not, with its number.
