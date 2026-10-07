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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan records a too_many_lines baseline (Phase 1), makes every showrunner reply end with the current footer through a Stop hook (Phase 2), adds a PostToolUse hook that blocks Claude edits leaving a Rust function over clippy's limit (Phase 3), fixes three production status defects the showrunner routed here (Phase 4), adds a stall watcher that bumps an idle unit and tells its showrunner (Phase 5), lets a showrunner turn its footers off and on with `/showrunner:footer`, pauses dailies and footers for an adhoc review, checks that Waiting on items lead with their ETA, keeps each tmux session named after its Claude session, and leaves nothing running after a killed verify (Phase 6), and lets the launcher carry follow-up work to a seat that is still open (Phase 7). The Codex hook moved to mul_add-unit as its Phase 4 (the showrunner, 2026-10-06 13:5x PDT); one command to start a unit and one to merge a checkpoint, with the script-or-not audit, moved to enh-showrunner (the showrunner, 2026-10-06 14:2x PDT). The re-measure moved to mul_add-unit as its Phase 6 (the showrunner, 2026-10-06 14:3x PDT). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
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
  - `scripts/hooks/post-tool-use-fn-length.py` — the hook entry for Claude payloads (Phase 3) and Codex `apply_patch` payloads (moved to mul_add-unit Phase 4) (new)
  - `scripts/hooks/test_fn_length.py` — its tests (new, Phase 3; its `apply_patch` cases are mul_add-unit Phase 4's)
  - `scripts/hooks/codex_hooks.py` — installs and trusts the Codex hook (mul_add-unit Phase 4)
  - `scripts/hooks/test_codex_hooks.py` — its tests, with a stub `codex` on `PATH` (mul_add-unit Phase 4)
  - `scripts/delegate/implement.sh` — `--to <seat>` follow-up mode; `scripts/delegate/test_implement_launcher.py` beside it (Phase 7)
  - `scripts/agents/codex_mesh.py` — `follow`; `scripts/agents/test_codex_mesh.py` beside it (Phase 7); `scripts/agents/agent_bg.sh` — attach to an open Claude seat (Phase 7)
  - `commands/unit/delegate.md`, `docs/delegate/write_prompt_contract.md` — follow-ups go through the launcher (Phase 7)
  - `settings.json` — Stop hook registration (Phase 2); Claude edit hook registration (Phase 3)
  - outside the repository, at run time: `~/.local/state/fn-length-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (mul_add-unit Phase 4); `~/.local/state/showrunner/footers-off/<slug>` and `~/.local/state/showrunner/review-paused/<slug>.json` (Phase 6)
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
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_stop_showrunner_footer.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` (Phase 2; the first again in Phase 6); `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` (Phase 3); `python3 -m unittest discover -s scripts/whoami -p 'test_run_out.py'` and `-s scripts/production -p 'test_unit_status.py'` (Phase 4); `python3 -m unittest discover -s scripts/production -p 'test_showrunners.py'`, `-p 'test_stall_watch.py'` and `-s scripts/whoami -p 'test_quota_alert.py'` and `-s scripts/message -p 'test_notifier.py'` (Phase 5); `-s scripts/production -p 'test_tmux_names.py'` and `-p 'test_review_pause.py'`, with `test_showrunners.py` and `test_unit_status.py` again, and `-s scripts/delegate -p 'test_verify_token_wait.py'` (Phase 6); `python3 -m unittest discover -s scripts/delegate -p 'test_implement_launcher.py'` and `-s scripts/agents -p 'test_codex_mesh.py'` (Phase 7); run from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has, so its exit status says nothing. `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust (showrunner, 2026-10-06).
- **Invariants:**
  - The showrunner footer hook fires only in a session a running production's `showrunner-*` notifier instance targets, and fails open: any error prints one stderr line and exits 0 with no output (user, 2026-10-06).
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced brace, an unknown payload or any internal error passes the edit. An internal error prints one `systemMessage` line (`fn-length hook error: <type>: <message>`) and exits 0 (plan author).
  - One checker serves both agents; the Codex hook calls the same script with the same command string as Claude's (user, 2026-10-06: "sharing one checker script").
  - A test never writes the real `~/.codex`, `~/.local/state/fn-length-hook` or `~/.local/state/buildlog`, never runs the real `codex` or `cargo`, and never edits `settings.json` beyond the Phase 3 registration (production rules). Live checks against real files are the unit director's, in the Acceptance gates.
  - Running `codex` from a Claude session needs `dangerouslyDisableSandbox`: codex writes `~/.codex` and fails with "Operation not permitted" otherwise (`/etc/nixos/modules/common/codex.nix`).
  - Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings (user rule, `~/.claude/CLAUDE.md`).
  - `~/.claude` main is the live configuration and each merged phase goes to it at once (production rule): the Claude hook goes live when Phase 3 reaches main, the Codex hook when mul_add-unit Phase 4's install runs on each machine.
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

**Binds later work:** the re-measure (mul_add-unit Phase 6) compares against 13.3 per 100, 4.9 per 100 and 1.07 h a day; its success limits are ≤ 3.3 per 100 and ≤ 0.27 h a day.

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

### Phase 6 — Showrunner footers turn off on demand and for an adhoc review, Waiting on items lead with their ETA, tmux names follow session names, and a killed verify leaves nothing running · status: done

#### As-built

- **Footer switch.** One file per production, `~/.local/state/showrunner/footers-off/<slug>` (present means footers off; absent, the default, means on), keyed like `outstanding/<slug>.json`, so it outlives compaction, `claude --resume` and a new `showrunner-<slug>` instance target; `SHOWRUNNER_STATE_DIR` replaces `~/.local/state/showrunner` in tests. `/showrunner:footer [on|off]` runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/showrunner_footer.py" off|on|status`, which finds the session's productions as the hook does (every `showrunner-*` instance whose conf has `TARGET=session:$CLAUDE_CODE_SESSION_ID`). `status` prints `<slug> footers off|on`, one line per production; `off` and `on` exit 0 when the switch already says so; with no such instance the script exits 1 with `no production targets this session`. Produce runs `/showrunner:footer` after every compaction and resume, and while it reports off ends each reply with the Waiting on block alone.
- **Hook with footers off.** A production whose switch is set needs no footer, but the reply still needs the Waiting on block (two empty lines, `Waiting on:`, an empty line, `* ` bullets); the block reason shows only that block and names `/showrunner:footer on`. A reply that still carries a footer is accepted. With several productions on one session, each is judged by its own switch. The switch is a named state (footers on, footers off), never a bare boolean or `None`; a `None` from `Production.next_due` or `FooterComparison.stamped` becomes a named state at that boundary. A session with no `showrunner-*` instance imports nothing extra.
- **Waiting on format,** checked with footers on or off. An ETA item leads with `[<Day> ]HH:MM[+1][ (HH:MM[+1]–HH:MM[+1])] - ` (`<Day>` a three-letter English weekday; `+1` or a weekday marks a time past midnight); a no-ETA item leads with `no ETA measured - `; any other bullet is the user's. The hook blocks an ETA mid-line (`ETA` plus a time, or an `HH:MM (HH:MM–HH:MM)` range, in a bullet that does not lead with a time), a zone after a leading time (an all-capitals word of two to five letters), leading times out of (day, minutes) order (unmarked is today, `+1` tomorrow, a weekday the next such day in the production's zone, today's own weekday being today), and kinds out of order (user, then ETA, then no-ETA). The reason names the broken rule and shows `19:45 (18:20–23:55) - startup Phase 16` and a `no ETA measured - <item>` line.
- **Adhoc review pause.** `scripts/production/review_pause.py pause | resume both|dailies|footers|none | status` acts on each production of the session. `pause` runs `NOTIFIER stop showrunner-<slug>` when the dailies are enabled and sets the footer switch when footers are on, recording what it changed in `~/.local/state/showrunner/review-paused/<slug>.json` (`{"dailies": true|false, "footers": true|false}`, written atomically); an existing record is kept and nothing changes. `resume` turns back on only what the record names and the answer selects, then deletes the record. `/adhoc_review` Step 2 runs `pause`; Step 5 runs `status` and, when a record exists, asks one line naming only what was paused (`Turn dailies and footers back on? (yes / dailies only / footers only / leave off)`), then runs `resume` (`yes` is `both`, `leave off` is `none`). A session no production targets sees none of it.
- **Tmux names follow session names.** `scripts/production/tmux_names.py`, run each minute by the run-only notifier instance `tmux-names`, reads each live session's `name` from `~/.claude/sessions/<pid>.json`, finds its tmux session through `TMUX_PANE` in `/proc/<pid>/environ`, and on a difference runs `tmux rename-session -t =<old> <new>` and `showrunners.py rename <old> <new>`. `rename` (replaces `rename-unit`) renames a unit entry and a showrunner's `session` alike under the config lock, and moves that name's stretch files under `~/.local/state/stall-watch/`, so a reported stretch is not bumped again. A tmux session hosting more than one Claude, and a new name already taken, are skipped and reported to `faults_to` once per case; no tmux server exits 0. `unit_status.sh <scratch> <zone> --showrunner <session>` reads a showrunner's units from `config/showrunners.json`; produce's `<StartUpdates/>` uses that form and creates `tmux-names` when `NOTIFIER status tmux-names` reports none. `showrunners.py import` reads the old prompt form as zone and units and the `--showrunner` form as zone only; `showrunners.py add` and `remove` are the only unit changes.
- **Stall watch.** `stall_watch.py`'s `claude_pid` counts the pane's own process first, then its descendants, so a pane started as `tmux new-session … claude` is watched.
- **Notifier run-only jobs.** `notifier.sh cmd_tick` starts each due `RUN=` job outside the tick: a transient systemd user service on Linux, a launchd job that removes its own label after the run on macOS, or, with neither launcher, a `run launcher unavailable` log line and a wait for the run after the tick lock is released. The job, its watchdog and its `fire.log` line outlive the tick.
- **Cargo token.** `verify.sh` runs each cargo step in its own recorded process group, and the waiting parent forwards INT/TERM/HUP to it. `board.sh release` and dead-holder reclaim in `board.sh acquire` stop that identity-checked group before the token moves, never a group the holder shares with its caller, and fail closed with `board.sh: step group still has running processes` when they cannot. An expired lock whose holder is alive is reclaimed without touching its build. A failed acquire exits 1. `test_verify_token_wait.py` leaves no process it started running.

**Files:**
- `commands/showrunner/footer.md` — `/showrunner:footer [on|off]`.
- `commands/showrunner/produce.md` — Footer section (the footer switch, the Waiting on rule, the adhoc pause); `<StartUpdates/>` `--showrunner` line and `tmux-names` instance.
- `commands/showrunner/promote_unit.md` — step 7 leaves the prompt's unit list alone.
- `commands/adhoc_review.md` — Steps 2 and 5 pause and resume.
- `scripts/hooks/showrunner_footer.py` — the switch, `off`/`on`/`status`, the footers-off and Waiting on checks; `scripts/hooks/stop-showrunner-footer.py` hands the switch state to the check; `scripts/hooks/test_stop_showrunner_footer.py`.
- `scripts/production/review_pause.py`, `scripts/production/test_review_pause.py`.
- `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py`.
- `scripts/production/showrunners.py` — `rename`, `import` of both prompt forms; `scripts/production/test_showrunners.py`.
- `scripts/production/unit_status.sh` — `--showrunner`; `scripts/production/test_unit_status.py`.
- `scripts/production/stall_watch.py` — stretch-file rename, a pane's own Claude process; `scripts/production/test_stall_watch.py`.
- `scripts/message/notifier.sh` — run-only job launch; `scripts/message/test_notifier.py`.
- `scripts/delegate/verify.sh`, `scripts/delegate/board.sh` — step-group ownership, release and reclaim; `scripts/delegate/test_verify_token_wait.py`.

**Binds later work:** `implement.sh` runs under the cargo-token rules: a failed acquire exits 1 and release fails closed when the step group cannot be inspected, so the launcher in "The launcher carries follow-up work to a seat that is still open, and a failed review pause can be retried" surfaces that exit and does not retry blindly. `verify.sh` still suppresses a `board.sh release` failure, so a fail-closed release is not yet visible to a seat. `review_pause.py pause` keeps any existing record and changes nothing, so a pause whose stop failed cannot be retried yet.

**Gotchas:**
- `launchctl submit` keeps its job alive (re-runs it every 10 s after exit 0), and launchd ends even `setsid` children when a job exits; a run that must outlive a launchd job needs its own job that removes itself.
- A systemd oneshot (`KillMode=control-group`) kills `&!` children of its `ExecStart`; under `session-notifier` this ended `stall_watch.py` mid-send, so a nudge went out unrecorded and went out again the next minute.
- Count leftover processes by process group, never `pgrep -f`.

**Ruled out:**
- Detaching a run with `setsid` inside a launchd job: launchd ends it.
- Running a build unguarded after a failed token acquire: overlapping builds are the memory-freeze cause.

### Moved: Codex seats get the same block after `apply_patch` (was Phase 7)

Moved to mul_add-unit as its Phase 4 (`docs/plans/build-followups-mul-add.md`) by the showrunner, 2026-10-06 13:5x PDT, so mul_add-unit can build it while this unit runs Phase 6 (the user's ask). The Work Order as written here is `git show dd2ee39:docs/plans/build-followups-fn-length-hook.md`, section Phase 7. While mul_add-unit holds them, this unit does not edit `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`, `scripts/hooks/post-tool-use-fn-length.py` or the `apply_patch` cases in `scripts/hooks/test_fn_length.py`; `scripts/hooks/fn_length_lib.py` stays this unit's.

### Moved: one command starts a unit (was Phase 7)

Moved to enh-showrunner (2026-10-06 14:2x PDT) by the showrunner (natedev), on the user's decision, with the next phase, because both automate showrunner steps. The showrunner copied the Work Order from this file. enh-showrunner edits `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md` and `scripts/production/showrunners.py` after Phase 6 merges.

### Moved: one command merges a checkpoint, and the script-or-not audit (was Phase 8)

Moved to enh-showrunner (2026-10-06 14:2x PDT) with the phase above; the showrunner copied its Work Order from this file.

### Phase 7 — The launcher carries follow-up work to a seat that is still open, and a failed review pause can be retried · status: done

#### As-built

- `implement.sh --to <seat>` (ahead of the usual positional arguments; the prompt file is the message) sends follow-up work to a seat still open in the phase. It skips `seat_name.sh` and addresses the full seat name from `mesh_roster.json` (Codex) or the `seats` ledger (Claude). It exits 2 with a message, before any record, when the seat is unknown, `mesh=none`, busy (roster `running` or `waiting_capacity`, `impl_status_<slot>` reads `implementing`, `claude agents` reads busy or gone), or its slot, family or configuration differs from the durable launch record (slot, family, model, full Claude session id or Codex thread id).
- Past those checks the launcher runs the new-seat path: truncates `impl_summary_<slot>.txt`, writes `impl_status_<slot>` (`implementing`, then `implemented` or `error`), calls `start-pass` and `finish-pass`, runs `findings.py landed` or `abandon --edits-landed` under `PLAN_DELEGATE_RESOLVES_ROUND=1`, and posts a register line (`role=<kind>`, `follow-up to <seat>`) and the `launcher:` `done` or `blocked` post. `done` posts only after the pass and landed state are recorded; a failed ledger write posts `blocked`. A fixed trailer on the message tells the seat to write `impl_summary_<slot>.txt` last, then post `done`. The launcher's exit wakes the unit director, as a new seat's does.
- One claim per seat. Codex: `codex_mesh.py can-follow --session-dir <dir> --to <seat> --claim-pid <pid>` checks the seat is followable and takes the roster claim under lock with the launcher pid before any status or pass write; a second concurrent follow-up exits 2 with nothing recorded. `codex_mesh.py follow --session-dir <dir> --to <seat> --message-file <path> [--claim-pid <pid>]` then requires that claim (a changed claim exits 1); without `--claim-pid` it refuses and claims on its own pid. A claim whose launcher is dead is released and its status set to `error`; `release-follow` releases an unstarted claim.
- `follow` accepts a roster entry that is followable (`done`, or `failed` only when `thread/read` shows no live turn). It starts or reuses the app-server, calls `thread/resume`, starts one turn on the same thread, and streams it through `_stream_turn`, the loop `start` also uses; it waits on the started turn id, so an earlier `done` cannot end the wait. A resume that fails, or a live turn found after resume, exits 1 with the reason. The roster record is a named state (followable, active with its turn id, starting with the launcher pid, waiting for capacity), and the live-server check returns `LiveServer` or `ServerRestartRequired` rather than `int | None`.
- Claude: `agent_bg.sh --attach` sends through `send.py` (`QUEUED` is an error), counts the seat's finished assistant turns in its transcript (`~/.claude/projects/*/<session id>.jsonl`) before sending, and waits until the count grows and the seat is idle; a turn shorter than the 15 s poll counts.
- Docs: a finished Codex seat is reached only through `implement.sh --to`; a repair whose files sit with an open seat may go to that seat the same way; a message to a seat without the launcher is for questions only. <DispatchContract/> item 6 and <FixDispatch/>'s third outcome name the follow-up beside a new seat.
- `review_pause.py` writes its pause record first, with one state per action (`dailies`, `footers`): `untouched`, `attempted`, `done`, `skipped`. `reconcile_attempted(instance, slug, path, record)`, shared by `pause`, `status` and `resume`, turns an `attempted` action into `done` when its effect is observable (dailies stopped, footers off); a failed notifier stop leaves it `attempted`, so a retry redoes only what did not happen, and `resume` undoes only the `done` actions (`parts`).
- `board.sh release <session_dir> <agent> <resource> --pid <pid>` refuses a pid that is not the holder's: exit 3 when the caller was reclaimed (listed in `reclaimed_holders`), else exit 1, with the reason on stderr. `verify.sh`'s `release_token` always passes `--pid $$`; 0 and 3 count as released (the reclaiming holder owns the token), any other status fails the run.
- `scripts/whoami/agent_accounts.py` reads `rateLimitResetCredits` through a key on the `CodexRateLimits` TypedDict and casts the reset-credit summary once in `reset_credits`; basedpyright reports 0 errors and 0 warnings with no ignore.

**Files:**
- `scripts/delegate/implement.sh` — launcher; the `--to` follow-up path.
- `scripts/agents/codex_mesh.py` — `can-follow`, `follow`, `release-follow`, roster claims and named states, `_stream_turn`.
- `scripts/agents/agent_bg.sh` — `--attach` to an open Claude seat.
- `scripts/production/review_pause.py` — pause, status and resume records with per-action states.
- `scripts/delegate/board.sh`, `scripts/delegate/verify.sh` — token release by holder pid; a failed release fails the run.
- `scripts/whoami/agent_accounts.py` — typed rate-limit reads.
- `commands/unit/delegate.md`, `docs/delegate/write_prompt_contract.md` — follow-up rules, read by every unit director.
- `scripts/delegate/test_implement_launcher.py`, `scripts/agents/test_codex_mesh.py`, `scripts/production/test_review_pause.py`, `scripts/delegate/test_verify_token_wait.py`, `scripts/delegate/test_board_reclaim.py` — `--to`, `follow` on `StubAppServer`, pause retry, release that fails closed.

**Binds later work:** the follow-up path shares the launcher's success path, so edits to `implement.sh`'s final status order must keep the follow-up claim, status and refusal behaviour; `codex_mesh.py`'s roster states are read by `can-follow`, `follow` and the launcher alike.

**Gotchas:**
- A follow-up claim must exist before any status record, or a refusal leaves a stale record.
- A peer turn can overtake a followed turn after `thread/resume`: `follow` exits 1, `impl_status` reads `error`, and the roster stays active while the peer runs, then is restored; a failure of the followed turn survives the peer turn.
- Legacy `false` pause actions read as skipped.
- `board.sh release` exit 3 is benign for `verify.sh`; exit 1 is a real failure.

### Phase 8 — `/unit:report off` and `on` stop and start a unit's progress updates, and the launcher reports done only after recording it · status: done

#### As-built

- `scripts/delegate/unit_notifier.sh <claude_session_id> [on|off]` reads the marker `${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}/<session id>`, which names the session directory; its basename is the run id. With no mode it creates the `delegate-<run id>` instance. `off` runs `notifier.sh stop delegate-<run id>` and `on` runs `notifier.sh start delegate-<run id>`; all three modes share the one marker lookup.
- On success it prints one line: `progress updates off: delegate-<run id>`, or `progress updates on: delegate-<run id>` followed by the next tick time `notifier.sh start` prints. A missing or empty marker exits 1 with its message and changes nothing; a `notifier.sh` failure (for example, no such instance) passes its message and exit status through; another mode word, or more than two arguments, prints the usage and exits 2.
- `/unit:report [on|off]` (with an `argument-hint`): `off` or `on` runs `zsh ~/.claude/scripts/delegate/unit_notifier.sh "$CLAUDE_CODE_SESSION_ID" off|on`, relays its line and composes no report; a failure is relayed as printed. A Codex unit has no notifier, so the command says the switch is unavailable there, as `commands/unit/interval.md` does. With no argument the command is unchanged.
- `scripts/delegate/implement.sh` writes `impl_status_<slot>` as `implemented` only after `finish-pass` and, under `PLAN_DELEGATE_RESOLVES_ROUND=1`, `findings.py landed` succeed; a failure in either writes `error`, posts `blocked` and exits 1. The worker-error path writes `error` after its `finish-pass` and `abandon --edits-landed`. Until the final write the file reads `implementing`.

**Files:**
- `scripts/delegate/unit_notifier.sh` — instance creation and the `on|off` switch.
- `commands/unit/report.md` — `[on|off]` usage and the switch paragraph before the contract.
- `scripts/delegate/implement.sh` — final status written after the pass and landed records.
- `scripts/delegate/test_delegate_check.py` — off/on round trip on an instance `unit_notifier.sh` created, missing and empty marker, bad arguments, missing-instance passthrough.
- `scripts/delegate/test_implement_launcher.py` — stub `finish-pass`, `findings.py landed` and `abandon` record the status file when they run and pin both orders.

**Binds later work:** `prepare_session.sh` calls `unit_notifier.sh` with the session id alone to create the instance, and that call stays unchanged (`test_unit_notifier_creates_held_instance_with_rounded_interval`). Unit directors and `launch_implementation.md` read `impl_status_<slot>`, so any new record step in `implement.sh` goes before the final status write. Tests run the real `notifier.sh` against temporary `NOTIFIER_STATE_DIR`, `PLAN_DELEGATE_ACTIVE_DIR`, `PLAN_DELEGATE_CONFIG` and `NOTIFIER_NOW_EPOCH`, as `test_delegate_check.py`'s `environment()` does, and never touch the real notifier state or send to a real session.

**Gotchas:**
- `unit_notifier.sh` exit 1 covers both a session with no active run and a run whose notifier instance is missing; only the message says which, so `/unit:report` relays it as printed.
- `off` persists in the notifier instance until `on` or `end_session.sh`.
- The switch is checked live by running `/unit:report off`, reading `notifier.sh status delegate-<run id>` as disabled, then `/unit:report on`.

**Ruled out:** naming `/unit:report off|on` in `commands/unit/delegate.md`'s `<ProgressContract/>`, which still names `notifier.sh stop|start`: the file is enh-showrunner's, and the showrunner clears any such line.

### Moved: re-measure after both hooks are live (was Phase 8)

Moved to mul_add-unit as its Phase 6 by the showrunner (natedev), 2026-10-06 14:3x PDT: that phase measures too_many_lines and suboptimal_flops over one fixed window with controls on both machines, after both fn-length hooks are live, so a second re-measure here would count the same lint twice. Phase 1's As-built keeps the baseline it reads.
