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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan records a too_many_lines baseline (Phase 1), makes every showrunner reply end with the current footer through a Stop hook (Phase 2), adds a PostToolUse hook that blocks Claude edits leaving a Rust function over clippy's limit (Phase 3), fixes three production status defects the showrunner routed here (Phase 4), adds a stall watcher that bumps an idle unit and tells its showrunner, and routes every repair through `implement.sh` (Phase 5), extends the hook to Codex seats (Phase 6), and re-measures (Phase 7). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
- **Project started:** 2026-10-06T15:12:44.327+00:00
- **Stack:** Python 3.13, standard library only (`json`, `re`, `tomllib`, `dataclasses`, `pathlib`, `subprocess`, `shlex`, `zoneinfo`, `unittest`); `scripts/lib/py` picks a Python ≥ 3.10 on each machine (3.13 on natedev and the Mac, so `tomllib` is present). Codex CLI 0.160.1 on natedev, 0.154.0 on the Mac.
- **Layout:**
  - `scripts/production/dailies_render.py` — `--footer` also prints the Agents lines (Phase 2), judged as of the stamp (Phase 4); `test_dailies_render.py` and `test_dailies_render_agents.py` beside it
  - `scripts/whoami/run_out.py` — readings log, trailing rate, run-out lean; `test_run_out.py` beside it (Phase 4)
  - `scripts/production/unit_status.sh` — the showrunner's per-unit status; `test_unit_status.py` beside it (Phase 4)
  - `config/showrunners.json` — shared showrunner config: quota-alert fields and `stall_minutes` (new, Phase 5; replaces `scripts/whoami/quota_alert.json`)
  - `scripts/production/showrunners.py` — the config, and the showrunners read from the `showrunner-*` notifier instances; `test_showrunners.py` beside it (new, Phase 5)
  - `scripts/production/stall_watch.py` — the stall watcher, run by the `stall-watch` notifier instance; `test_stall_watch.py` beside it (new, Phase 5)
  - `scripts/whoami/quota_alert.py` — reads the shared config; `test_quota_alert.py` beside it (Phase 5)
  - `scripts/hooks/stop-showrunner-footer.py` — the Stop hook entry; `scripts/hooks/showrunner_footer.py` — production lookup, footer render, comparison; `scripts/hooks/test_stop_showrunner_footer.py` — their tests (new, Phase 2)
  - `commands/showrunner/produce.md` — the Footer section (Phase 2)
  - `scripts/hooks/fn_length_lib.py` — the checker: lint scope, function discovery, clippy's count (new, Phase 3)
  - `scripts/hooks/post-tool-use-fn-length.py` — the hook entry for Claude payloads (Phase 3) and Codex `apply_patch` payloads (Phase 6) (new)
  - `scripts/hooks/test_fn_length.py` — its tests (new, Phase 3; extended Phase 6)
  - `scripts/hooks/codex_hooks.py` — installs and trusts the Codex hook (new, Phase 6)
  - `scripts/hooks/test_codex_hooks.py` — its tests, with a stub `codex` on `PATH` (new, Phase 6)
  - `settings.json` — Stop hook registration (Phase 2); Claude edit hook registration (Phase 3)
  - outside the repository, at run time: `~/.local/state/fn-length-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (Phase 6)
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
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_stop_showrunner_footer.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` (Phase 2); `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` and `-p 'test_codex_hooks.py'` (Phases 3 and 6); `python3 -m unittest discover -s scripts/whoami -p 'test_run_out.py'` and `-s scripts/production -p 'test_unit_status.py'` (Phase 4); `python3 -m unittest discover -s scripts/production -p 'test_showrunners.py'`, `-p 'test_stall_watch.py'` and `-s scripts/whoami -p 'test_quota_alert.py'` and `-s scripts/message -p 'test_notifier.py'` (Phase 5); run from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has, so its exit status says nothing. `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust (showrunner, 2026-10-06).
- **Invariants:**
  - The showrunner footer hook fires only in a session a running production's `showrunner-*` notifier instance targets, and fails open: any error prints one stderr line and exits 0 with no output (user, 2026-10-06).
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced brace, an unknown payload or any internal error passes the edit. An internal error prints one `systemMessage` line (`fn-length hook error: <type>: <message>`) and exits 0 (plan author).
  - One checker serves both agents; the Codex hook calls the same script with the same command string as Claude's (user, 2026-10-06: "sharing one checker script").
  - A test never writes the real `~/.codex`, `~/.local/state/fn-length-hook` or `~/.local/state/buildlog`, never runs the real `codex` or `cargo`, and never edits `settings.json` beyond the Phase 3 registration (production rules). Live checks against real files are the unit director's, in the Acceptance gates.
  - Running `codex` from a Claude session needs `dangerouslyDisableSandbox`: codex writes `~/.codex` and fails with "Operation not permitted" otherwise (`/etc/nixos/modules/common/codex.nix`).
  - Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings (user rule, `~/.claude/CLAUDE.md`).
  - `~/.claude` main is the live configuration and each merged phase goes to it at once (production rule): the Claude hook goes live when Phase 3 reaches main, the Codex hook when Phase 6's install runs on each machine.
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

### Phase 5 — A stall watcher bumps an idle unit and tells its showrunner · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 11:59–12:12 PDT, through the showrunner (natedev), growing its 11:20 PDT stalled-unit report; three additions from the showrunner at 12:50 PDT. Measured: at 10:52 PDT this unit ended a turn on a `holding:` line with nothing running and sat idle about 40 minutes; organon (tool-based-ui) had a seat finish at 10:57 PDT and nothing woke it until 12:00, after a repair sent to the seat by message skipped the recorder and the ledger. Nothing outside either session noticed, and nothing could have woken them.

**Goal:** every unit of every running production is checked once a minute from outside any session, from the moment this phase is installed; a unit idle 5 minutes with nothing running is sent a message telling it to continue, and its showrunner is told, once per idle stretch. A showrunner is known by the update instance `/showrunner:produce` already creates, so a new one is covered with no other step. A repair always goes through `implement.sh`, never to a seat by message.

**Spec:**
- **Shared showrunner config.** `config/showrunners.json` (tracked) replaces `scripts/whoami/quota_alert.json`: `threshold_percent`, `repeat_minutes` and `notify` move there unchanged, plus `stall_minutes: 5`.
- **The showrunners are the `showrunner-*` notifier instances.** `/showrunner:produce` `<StartUpdates/>` creates `showrunner-<slug>` on start and every resume, and `<Wrap/>` removes it, as does its check once the doc says `wrapped` (`commands/showrunner/produce.md:236-262`, `:923`); so the set of instances is the set of running productions, with no second registry and nothing to register. Each instance's `conf` (`~/.local/state/notifier/<instance>/conf`) gives the showrunner's session (`TARGET=session:<id>`) and its prompt file (`PROMPT_FILE=…`); the prompt's `unit_status.sh <scratch> <zone> <tmux session…>` command (`produce.md:258`) gives the zone and every unit's tmux session, the same list the showrunner's own status run uses. At 12:50 PDT the instances were `showrunner-build-followups` (units `hook`, `mul_add`), `showrunner-tool-based-ui` (six units, organon in `tool-based-ui-geometry-material`) and `showrunner-pr-13-gamepads` (`gamepad`), so every running production is covered the moment the watcher runs. `scripts/production/showrunners.py` owns both reads, converting at the read boundary into `ShowrunnerSettings` (the config) and `RunningShowrunner` (slug, session target, zone, unit sessions); it has a `list` command. `SHOWRUNNERS_CONFIG` and `NOTIFIER_STATE_DIR` (already `notifier.sh`'s) override the two paths, so tests use temporary ones. An instance whose conf or prompt lacks a field is skipped with one stderr line.
- **Quota alerts read it.** `quota_alert.py` loads `ShowrunnerSettings` through `showrunners.py`, and every path that sends to `notify` today — the alert (`quota_alert.py:408`), the restored notice (`:421`) and `tell_others` (`:470`) — sends to the union of `notify` and every running showrunner's session, once per recipient: each is resolved with `sessions.py socket <name|session:id>` and sent to that socket, so a showrunner also named in `notify` is not told twice. Behavior is otherwise unchanged. `scripts/whoami/quota_alert.json` is deleted; `docs/quota_alerts.md` names the new file.
- **Produce starts the watcher.** `commands/showrunner/produce.md` `<StartUpdates/>` gains one step: create the `stall-watch` notifier instance when it does not exist. `<QuotaAlert/>`'s hand check of the `notify` list is replaced by a sentence saying every showrunner with an update instance is alerted. `<Wrap/>` is unchanged: removing the update instance ends the watch.
- **Installed at merge.** The unit director creates the `stall-watch` instance once this phase reaches `~/.claude` main, so the running productions are watched at once, not at each showrunner's next resume.
- **The watcher runs outside every session.** `notifier.sh` gains a run-only instance: `new <instance> --every <min> --run <cmd> [--timeout <s>]`, which takes no `--to`, `--command`, `--prompt-file` or `--check`; each due tick runs `<cmd>` under the instance timeout and sends nothing; a non-zero exit or a timeout is one line in the instance's `fire.log`, a zero exit writes nothing; `status` prints `<instance> runs <cmd> every <N> min`. The watcher is `scripts/production/stall_watch.py`, run by one instance `stall-watch` (`notifier.sh new stall-watch --every 1 --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/stall_watch.py"`; the notifier ticks every 15 s on both machines). It sends its own messages. A tick takes a non-blocking lock on `~/.local/state/stall-watch/lock` and exits 0 at once when an earlier tick still holds it. Never a hook.
- **Which units.** Every unit session of every running showrunner whose tmux session exists. Claude is found through the tmux pane's descendants as `unit_status.sh` does (`#{pane_pid}`, breadth-first, first whose args begin `claude `), then `scripts/message/sessions.py id <pid>` and `~/.claude/sessions/<pid>.json` (`messagingSocketPath`), never through Claude's own session name: organon runs in tmux session `tool-based-ui-geometry-material`, and this unit's remote-control name has differed from its tmux session.
- **Stalled means all of these, for `stall_minutes`:** the pane's text is unchanged (the watcher keeps a hash and the time it last changed per unit in `~/.local/state/stall-watch/`); the session transcript (`~/.claude/projects/*/<session id>.jsonl`) and every file under its `subagents/` directory are unmodified; no process descends from the Claude pid whose command is a shell (`zsh`, `bash`, `sh`) or a delegate launcher (`implement.sh`, `review.sh`, `verify.sh`). Only a live process counts as running work: a delegate status file that says `implementing` with no launcher or seat process alive is no evidence (`scripts/hooks/delegate_run.py:97` reads status text alone), so the watcher does not consult it. That is the minimum: a unit waiting at a gate is flagged too.
- **On a stall, once per idle stretch** (a pane change starts a new stretch): (1) bump the unit: `send.py --to uds:<messagingSocketPath> --from stall-watch` with "stall-watch: you have been idle since <HH:MM zone> with nothing running. Continue your run; if you are waiting on someone, say on whom in one line."; (2) tell its showrunner: `send.py --to uds:<socket from sessions.py socket session:<id>> --from stall-watch` with "<unit> idle since <HH:MM zone>, nothing running; bumped. Last status: <the pane's latest turn-end line, `(— )?(holding|gate|decision|blocked|done):…`, or `none on screen`>". Times are in the production's zone. Each send carries `--key stall-watch:<unit>:<stretch start>:<bump|tell>`. The two recipients' outcomes are kept apart: a recipient whose send exited 0 is never sent again in that stretch, and only a recipient whose send failed is retried on the next tick, with the same key. A queued `uds:` message is read by nobody (`send.py:20-37`; only `scripts/prioritize/run_watcher.sh` reads a queue), so a retry cannot deliver twice. All of a tick's sends start together and the tick waits at most 90 s for them; a send still running then is stopped and counts as failed.
- **A repair goes through `implement.sh`.** `commands/unit/delegate.md` `<FixDispatch/>` gains one rule: every repair, a follow-up to a seat that is still open included, is a dispatch through `implement.sh`; repair work is never sent to a seat by message (`codex_mesh.py send`, SendMessage). A message-borne repair skips the pass recorder and the findings ledger, and nothing wakes the unit director when it ends (organon, tool-based-ui, 2026-10-06 10:57–12:00 PDT). Questions to a seat still go by message (`docs/delegate/write_prompt_contract.md:184`).

**Files:**
- `config/showrunners.json` — new; `scripts/whoami/quota_alert.json` — deleted.
- `scripts/production/showrunners.py` — new: `ShowrunnerSettings`, and the `RunningShowrunner` list read from the notifier instances and their prompt files.
- `scripts/production/test_showrunners.py` — new.
- `scripts/whoami/quota_alert.py`; `scripts/whoami/test_quota_alert.py`; `docs/quota_alerts.md`
- `scripts/production/stall_watch.py` — new.
- `scripts/production/test_stall_watch.py` — new.
- `scripts/message/notifier.sh` — the run-only instance; `scripts/message/test_notifier.py`
- `commands/showrunner/produce.md` — `<StartUpdates/>`, `<QuotaAlert/>`.
- `commands/unit/delegate.md` — `<FixDispatch/>` (also touches; owner followups-unit, run done).

**Seats:** 1 writer + 1 tester.
- `impl` — `config/showrunners.json`, `scripts/production/showrunners.py`, `scripts/whoami/quota_alert.py`, `docs/quota_alerts.md`, `scripts/production/stall_watch.py`, `scripts/message/notifier.sh`, `commands/showrunner/produce.md`, `commands/unit/delegate.md`, and the deletion of `scripts/whoami/quota_alert.json`.
- `test` — `scripts/production/test_showrunners.py`, `scripts/whoami/test_quota_alert.py`, `scripts/production/test_stall_watch.py`, `scripts/message/test_notifier.py`, written from the Spec alone, with stub `tmux`, `ps` and `send.py`, a temporary notifier state directory, config and transcript files; never the real `~/.local/state`, sessions or sockets:
  - showrunners: read from `showrunner-*` instance confs and their prompt files (session target, zone, unit sessions); another instance name is ignored; a conf or prompt missing a field is skipped; quota alerts, restored notices and `tell_others` reach `notify` plus every showrunner session, once each when a `notify` name and an instance resolve to one socket;
  - a stalled unit is bumped and its showrunner told once, and not again on the next ticks of the same stretch; a pane change then a new 5-minute stretch reports again; when the bump arrives and the showrunner's message fails, the next tick resends only the showrunner's message; three stalled units with a stub `send.py` that takes 20 s are all sent within one tick; a tick that finds the lock held exits 0 and sends nothing;
  - not stalled: a unit with a running shell, with an `implement.sh` seat, or with a transcript or subagent file modified within 5 minutes; stalled: a unit whose delegate status says `implementing` with no live process;
  - a new `showrunner-<slug>` instance is covered on the next tick with no other step; a removed one is not;
  - the bump goes to the unit's own socket from `~/.claude/sessions/<pid>.json` even when its remote-control name differs from its tmux session.
  - notifier: a `--run` instance runs its command once per due tick, sends nothing, logs a non-zero exit and a timeout to `fire.log` and a zero exit not at all; `new` refuses `--run` beside `--to`, `--command`, `--prompt-file` or `--check`.

**Constraints from prior phases:**
- Phase 4 (as built): `unit_status.sh` finds Claude through the pane; port that walk, do not call the script.
- Production rules: never edit `/etc/nixos` or `settings.json`; tests never run the real `tmux`, `ps` or `send.py` against live sessions.

**Acceptance gate:**
- From the worktree root: `python3 -m unittest discover -s scripts/production -p 'test_showrunners.py'`, `-p 'test_stall_watch.py'`, `python3 -m unittest discover -s scripts/whoami -p 'test_quota_alert.py'`, `python3 -m unittest discover -s scripts/message -p 'test_notifier.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files; `zsh -n scripts/message/notifier.sh`.
- Live (unit director, once the merge reaches `~/.claude` main): create the `stall-watch` instance; `showrunners.py list` names build-followups, tool-based-ui and pr-13-gamepads with no showrunner resumed; `notifier.sh status stall-watch` shows the instance; a scratch unit session left idle 6 minutes receives the bump and natedev receives the report once.

### Phase 6 — Codex seats get the same block after `apply_patch` · status: todo

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

### Phase 7 — Re-measure four days after both hooks are live · status: todo

#### Work Order

**Starts:** 96 hours after T_codex (Phase 6's As-built) — a wait on the clock, not a production gate. The window must hold no hours from before T_codex.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Goal:** the plan states whether the hook cut too_many_lines failures at the source, against Phase 1's baseline and a stated threshold, with proof that both hooks fired.

**Spec:**
- Confirm the four script hashes: `sha256sum` in `~/.local/state/nightly-review/2026-10-06/work/lints/` gives `c2de20870ab2de9b5438e469c565d25c48185438ab42dd77807d817655b5d4fa cost.py`, `a775765b96311a783c8d6af8cdc925071b89b4ac423ae8ffa75b88fe81bf9abf loop.py`, `e178642d9bb1690562c132d824256198cc8f6e622b779b94ca76abbc75e841ce clippy_fail_lints.py`, `18754c36c8c545928a082e3bded49c688f029221b917b0f1e6c164028e4b6522 tml_lengths.py`; a mismatch stops the phase. Run, in the background with `set -o pipefail`, from `~/.local/state/nightly-review/2026-10-06/work/lints/`: `python3 cost.py 4 too_many_lines`, `python3 loop.py 4 too_many_lines`, `python3 clippy_fail_lints.py 4`, `python3 tml_lengths.py 4` (only when `clippy_fail_lints.py` reports at least one too_many_lines failure; it indexes an empty result, so with none record zero lengths instead), and the totals query `select count(*), sum(status!=0), min(started_at) from steps where repo='hana' and step='clippy' and started_at>=datetime('now','-4 days')` against `~/.local/state/buildlog/index.sqlite` opened read-only (`?mode=ro`), between T_codex + 96 h and T_codex + 100 h, so the 4-day window starts after T_codex.
- Derive the same three numbers as Phase 1: too_many_lines-including failed steps (`clippy_fail_lints.py`) per 100 hana clippy steps; sole-cause failed steps (`cost.py`) per 100; sole-cause seat time per day = (`loop.py` failed-call wall + repair-gap sum) / span days, the span running from the window's first hana clippy step to the run.
- **Success** when all three hold: too_many_lines-including failed steps per 100 hana clippy steps ≤ 25% of Phase 1's rate (13.3 per 100, so ≤ 3.3); sole-cause seat time per day ≤ 25% of Phase 1's (1.07 h, so ≤ 0.27 h); and the control — `blocks.jsonl` holds at least one `"agent": "claude"` and one `"agent": "codex"` line inside the window, counting natedev's file and the Mac's read over `ssh mac`. Earlier smokes may fall outside the window, so before the run the unit director makes one 101-line edit through each agent inside it (a Claude `Write`, a Codex `apply_patch`) and confirms both the visible block and its log line. A failed control means a hook did not fire, and the rates say nothing about the hook; a block with no log line leaves the control unproven, since a failed log append is silent.
- Residuals: for each too_many_lines diagnostic in the window's failed steps (file and line from the step log), read the step log and the buildlog row read-only and match it to a `blocks.jsonl` record with the same worktree (`cwd`), file and function name, earlier than the step's start. Matched: the hook fired and the agent linted before splitting. Unmatched: an edit the hook did not see (a shell edit, rustfmt growth past the limit, or a person), a function inside a `macro_rules!` body (never measured), or `unknown` when the logs cannot say. Report each count and the five most frequent files.
- Write the numbers, the verdict and the residual counts into this phase's As-built, and send the showrunner the verdict line.

**Files:**
- `docs/plans/build-followups-fn-length-hook.md` — this phase's As-built (the closeout writes it).

**Seats:** 1 writer — `impl` runs the commands and reports; nothing splits and there is no code or test lane.

**Constraints from prior phases:**
- Phase 1's As-built holds the baseline: 13.3 too_many_lines failed steps per 100 hana clippy steps, 4.9 sole-cause per 100, 1.07 h sole-cause seat time a day (run 2026-10-06 08:17 PDT, span 3.96 days). Phase 3's As-built holds T_claude, Phase 6's T_codex.
- Phase 3 (as built): functions inside a `macro_rules!` or `name! { … }` body are never measured, and `cfg(test)` modules in a package-root `examples/` target are skipped (examples are not built in test mode); a failed `blocks.jsonl` append is silent.
- The scripts and the buildlog are read-only (user, 2026-10-06).
- Saved run output stays under a few GB: read each run and delete it before the next.

**Acceptance gate:**
- All hashes match, every command exits 0, and the As-built states each of the three success conditions as met or not, with its number.
