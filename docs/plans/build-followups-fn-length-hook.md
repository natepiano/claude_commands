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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan records a too_many_lines baseline (Phase 1), adds a PostToolUse hook that blocks Claude edits leaving a Rust function over clippy's limit (Phase 2), extends it to Codex seats (Phase 3), and re-measures (Phase 4). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
- **Project started:** 2026-10-06T15:12:44.327+00:00
- **Stack:** Python 3.13, standard library only (`json`, `re`, `tomllib`, `dataclasses`, `pathlib`, `subprocess`, `unittest`); `scripts/lib/py` picks a Python ≥ 3.10 on each machine (3.13 on natedev and the Mac, so `tomllib` is present). Codex CLI 0.160.1 on natedev, 0.154.0 on the Mac.
- **Layout:**
  - `scripts/hooks/fn_length_lib.py` — the checker: lint scope, function discovery, clippy's count (new, Phase 2)
  - `scripts/hooks/post-tool-use-fn-length.py` — the hook entry for Claude payloads (Phase 2) and Codex `apply_patch` payloads (Phase 3) (new)
  - `scripts/hooks/test_fn_length.py` — its tests (new, Phase 2; extended Phase 3)
  - `scripts/hooks/codex_hooks.py` — installs and trusts the Codex hook (new, Phase 3)
  - `scripts/hooks/test_codex_hooks.py` — its tests, with a stub `codex` on `PATH` (new, Phase 3)
  - `settings.json` — Claude hook registration (Phase 2)
  - outside the repository, at run time: `~/.local/state/fn-length-hook/blocks.jsonl`; `~/.codex/hooks.json` and `~/.codex/config.toml` `[hooks.state]` on each machine (Phase 3)
- **Key files:**
  - `scripts/hooks/post-tool-use-banned-words.py` — the blocking convention: one JSON object with `decision: "block"`, a short `reason`, `continue: true`, a one-line `systemMessage`, and `hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext}`; read-only tools exit before heavy imports
  - `scripts/hooks/banned_words_lib.py` — the sibling-library convention the new library follows
  - `scripts/hooks/post-tool-use-basedpyright.py` — the `Edit|MultiEdit|Write` hook; `HookInput`/`ToolInput` TypedDicts for the Claude payload (`tool_input.file_path`)
  - `scripts/hooks/test_brp_launch_gate.py` — hook test convention: runs the hook as a subprocess, `setUp` with `@override` and `enterContext(tempfile.TemporaryDirectory())`, environment overrides, reads `settings.json` through `SETTINGS = HOOK.parent.parent.parent / "settings.json"`
  - `settings.json` — `hooks.PostToolUse` has a group `{"matcher": "Edit|MultiEdit|Write", "hooks": [<basedpyright>]}`; commands spell `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/<script>"`
  - `scripts/agents/codex_mesh.py:256-319` — JSON-RPC client convention (`initialize` with `clientInfo` and `capabilities: {"experimentalApi": true}`, string request ids, `_require`); read only, unchanged by this plan
  - `pyrightconfig.json` — `scripts/hooks` is an execution environment with itself on `extraPaths`, so tests import `fn_length_lib` directly
  - `~/.local/state/nightly-review/2026-10-06/work/lints/{cost.py,loop.py,clippy_fail_lints.py,tml_lengths.py}` — the measurement scripts, read-only over `~/.local/state/buildlog/index.sqlite` and its gzip step logs; outside the repository and never edited (user, 2026-10-06: "using … cost.py, loop.py and clippy_fail_lints.py")
- **Test lanes:** `scripts/hooks/` — `test_*.py` beside the scripts; this repository has no `tests/` directories.
- **Build:** none — Python and JSON; nothing compiles.
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` and `-p 'test_codex_hooks.py'`, run from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has, so its exit status says nothing. `python3 -m json.tool settings.json > /dev/null` after editing `settings.json`.
- **Style:** none — not Rust (showrunner, 2026-10-06).
- **Invariants:**
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced brace, an unknown payload or any internal error passes the edit. An internal error prints one `systemMessage` line (`fn-length hook error: <type>: <message>`) and exits 0 (plan author).
  - One checker serves both agents; the Codex hook calls the same script with the same command string as Claude's (user, 2026-10-06: "sharing one checker script").
  - A test never writes the real `~/.codex`, `~/.local/state/fn-length-hook` or `~/.local/state/buildlog`, never runs the real `codex` or `cargo`, and never edits `settings.json` beyond the Phase 2 registration (production rules). Live checks against real files are the unit director's, in the Acceptance gates.
  - Running `codex` from a Claude session needs `dangerouslyDisableSandbox`: codex writes `~/.codex` and fails with "Operation not permitted" otherwise (`/etc/nixos/modules/common/codex.nix`).
  - Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings (user rule, `~/.claude/CLAUDE.md`).
  - `~/.claude` main is the live configuration and each merged phase goes to it at once (production rule): the Claude hook goes live when Phase 2 reaches main, the Codex hook when Phase 3's install runs on each machine.
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

### Phase 2 — Claude edits that leave a function over the limit are blocked at once · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 06:2x PDT (see Source above).

**Goal:** after an `Edit`, `MultiEdit` or `Write` to a `.rs` file in a package whose Cargo lints enable `too_many_lines`, the agent is told `fn <name> at <file>:<line> is <N> lines (limit <T>): split it now` for each non-exempt function over the limit, with the count clippy would report.

**Spec:**
- `fn_length_lib.py` (typed; `from __future__ import annotations`; frozen dataclasses):
  - `@dataclass(frozen=True) class FunctionLength: name: str; line: int; lines: int; exempt: bool` — `line` is the 1-based line of the `fn` keyword; `lines` is clippy's count.
  - `@dataclass(frozen=True) class LintScope: enabled: bool; threshold: int`.
  - `def count_body_lines(body: str) -> int` — the exact port of clippy's loop (Source facts above) over the text strictly between the body's braces: `.strip()`, split on `"\n"` with a trailing `"\r"` removed per line, `in_comment` carried across lines, `str.find` for `/*`, `//` and `*/`. Ported quirks are kept: strings are not parsed, and a line that begins with a comment counts only if code follows a closed `/* */` on it.
  - `def measure_functions(source: str) -> list[FunctionLength]` — one forward scan with a compiled regex tokenizer over: line comments; block comments, which nest in Rust; string literals (`"…"` with escapes, `b"…"`, `c"…"`, raw `r#*"…"#*` with any `#` count, `br`/`cr` forms); char literals (`'x'`, `'\n'`, `'\u{…}'`, `b'x'`) kept apart from lifetimes (`'a`, `'static`); `{` `}` `(` `)` `[` `]` `;`; `#[` and `#![` attributes captured to their balanced `]`; the keywords `fn impl trait mod`; macro invocations `ident!` and `macro_rules!`.
    - A function is `fn` followed by an identifier; `fn(` (pointer type) and `fn $name` (macro metavariable) are not. Its signature ends at the first `{` or `;` at paren and bracket depth 0; `;` means no body (trait or extern declaration). Its body runs to the matching `}`; `count_body_lines` gets the raw source between.
    - Exemption: an attribute exempts when its path is `allow` or `expect`, or `cfg_attr(<predicate>, …)` containing `allow(` or `expect(`, and its list names `clippy::too_many_lines` or `clippy::pedantic` (whitespace and line breaks inside the attribute ignored). Outer attributes pending since the last `;`, `{` or `}` attach to the next item: on a `fn`, they exempt it; on an `impl`, `trait`, `mod` or `fn` whose block opens, they exempt every function inside. An inner `#![…]` exempts the rest of its enclosing block, or the whole file at top level.
    - Nested functions are measured on their own; their lines also count in the enclosing body. Closures are never measured. Functions inside a macro invocation's or `macro_rules!` delimiters are not measured.
    - Unbalanced braces: functions whose body never closes are dropped, and the rest of the result stands.
  - `def lint_scope(rs_file: Path) -> LintScope` — the package is the nearest ancestor directory whose `Cargo.toml` has a `[package]` table; none → disabled. If `lints.workspace = true`, read `[workspace.lints.clippy]` from `package.workspace`'s path when set, else from the nearest ancestor `Cargo.toml` (the package's own included) that has `[workspace]`; otherwise read the package's `[lints.clippy]`. A level is a string or a table's `level`. `too_many_lines` decides when present, else `pedantic`; enabled when the level is `warn`, `deny` or `forbid`. Threshold: the first `clippy.toml` or `.clippy.toml` from the package directory upward with `too-many-lines-threshold`, else 100. Any read or parse error → disabled.
  - `def long_functions(rs_file: Path) -> tuple[LintScope, list[FunctionLength]]` — the non-exempt functions with `lines > threshold`, in source order; an empty list when the scope is disabled or the file cannot be read as UTF-8.
- `post-tool-use-fn-length.py` (executable, `#!/usr/bin/env python3`):
  - Reads the payload from stdin. Claude tools `Edit`, `MultiEdit`, `Write` give `tool_input.file_path`. Any other tool, a path not ending `.rs`, or a missing file → exit 0, no output, before importing `fn_length_lib` or `re`.
  - On long functions, prints one JSON object and exits 0: `decision: "block"`; `reason`: each function as `fn <name> at <path>:<line> is <N> lines (limit <T>)` joined by `; `, then `: split it now. The edit was applied.` (one function: `fn render at crates/hana/src/x.rs:120 is 134 lines (limit 100): split it now. The edit was applied.`), where `<path>` is relative to the payload's `cwd` when inside it, else absolute; `continue: true`; `systemMessage`: `fn-length: <file name> has <k> function(s) over <T> lines`; `hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext}` — the same list, one function per line, and: split each into named helpers that each do one thing; the limit is clippy's too_many_lines, which fails `verify.sh lint`.
  - Every block appends one line to `<state>/blocks.jsonl`, `<state>` = `FN_LENGTH_HOOK_STATE` when set and non-empty, else `~/.local/state/fn-length-hook` (created 0700): `{"at": <UTC ISO-8601>, "agent": "claude", "tool": <tool_name>, "cwd": <cwd>, "file": <absolute path>, "threshold": <T>, "functions": [{"name", "line", "lines"}]}`. A failed append never changes the verdict.
  - Internal errors follow the Invariant: one `systemMessage`, exit 0.
- `settings.json`: append `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py\""}` to the existing `Edit|MultiEdit|Write` group's `hooks` list after basedpyright; nothing else in the file changes.
- Speed budget, measured on natedev: inside the process, read + scan + count + scope lookup p95 ≤ 10 ms over hana's 1,541 `.rs` files and ≤ 100 ms on `crates/hana_catalyst/src/assembly_operations.rs` (17,321 lines); end to end through `lib/py`, the median of 30 hook runs on a 2,154-line file minus the median of 30 `lib/py -c 'import json, sys'` runs in the same session ≤ 20 ms; a non-`.rs` payload adds ≤ 5 ms to that bare launch.

**Files:**
- `scripts/hooks/fn_length_lib.py` — new: the checker.
- `scripts/hooks/post-tool-use-fn-length.py` — new: the hook entry.
- `scripts/hooks/test_fn_length.py` — new: tests.
- `settings.json` — one handler appended to the `Edit|MultiEdit|Write` group.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/hooks/fn_length_lib.py`, `scripts/hooks/post-tool-use-fn-length.py`, `settings.json`.
- `test` — `scripts/hooks/test_fn_length.py`, written from the Spec alone; library cases import `fn_length_lib`, payload cases run the hook as a subprocess with `HOME` and `FN_LENGTH_HOOK_STATE` in a temporary directory and a temporary crate (`Cargo.toml` with `[package]` and `[lints] workspace = true` under a workspace denying `pedantic`):
  - counting: blank and comment-only lines skipped; a block comment spanning lines; `/* c */ code()` counts; `code(); // c` counts; a line holding only `"http://x"` counts; leading and trailing blank lines inside the braces; a one-line `{ x }` body is 1;
  - boundary: 100 counted lines pass, 101 block, and a `clippy.toml` threshold of 50 moves the boundary;
  - exemptions: `#[allow(clippy::too_many_lines)]`; a multi-line `#[expect(clippy::too_many_lines, reason = "…")]`; `#[allow(clippy::pedantic)]`; on an `impl`; on `mod tests`; `#![allow(clippy::too_many_lines)]` at file top; `cfg_attr(test, allow(clippy::too_many_lines))`; `#[allow(clippy::too_many_arguments)]` does not exempt;
  - discovery: a nested fn measured alone and counted in its parent; a trait method without a body skipped; `fn(u8) -> u8` types ignored; closures not measured; functions inside `macro_rules!` and `name! { … }` not measured; braces inside strings, raw strings with `#`, char literals `'{'`, and nested block comments leave the scan balanced; `'a` lifetimes do not open a char literal;
  - scope: `too_many_lines = "allow"` disables; no `[lints]` disables; no `Cargo.toml` disables; malformed TOML disables;
  - hook: an `Edit` payload over the limit prints the exact `reason` text and one log line with `"agent": "claude"`; `Write` and `MultiEdit` payloads; a short function prints nothing; a `.py` path prints nothing and leaves no log; malformed stdin exits 0 with no `decision`; `settings.json` lists the new command in the `Edit|MultiEdit|Write` group after basedpyright.

**Constraints from prior phases:** this phase touches no Phase 1 file.
- Tests run on synthetic crates in temporary directories only, never on `~/rust`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'` green; basedpyright 0 errors and 0 warnings on the three `.py` files; `python3 -m json.tool settings.json` succeeds.
- Parity with clippy (unit director, natedev): copy `~/rust/obsidian_knife` without `target/` into the scratchpad, add `clippy.toml` with `too-many-lines-threshold = 0`, run `cargo clippy --all-targets --message-format=json -- -A clippy::all -W clippy::too_many_lines` in the background, and compare each reported `this function has too many lines (N/0)` against `measure_functions` for that file: every function clippy reports matches in name and count, and each function the checker measures that clippy does not report is under a `#[cfg]` or inside a macro, listed in the checkpoint. Delete the copy after.
- Control on real code: extract `git -C ~/rust/hana archive origin/main` into the scratchpad (never read the live checkout, whose working tree belongs to another session) and run `long_functions` over every `.rs` file in it (main passes clippy): zero non-exempt functions over 100, and the count of exempt functions over 100 is above zero (the attribute grep finds 24 files naming too_many_lines); report both numbers.
- The speed budget above holds; report the numbers.
- Live smoke (unit director, after the merge reaches `~/.claude` main, from a Claude session started after that merge): in a scratchpad crate denying `pedantic`, `Write` a 101-line function; the block reason appears and `~/.local/state/fn-length-hook/blocks.jsonl` gains an `"agent": "claude"` line. Record the merge time as T_claude in PDT. Delete the crate.

### Phase 3 — Codex seats get the same block after `apply_patch` · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Source:** the user, 2026-10-06 06:2x PDT: "i am 99% sure that our hooks propagate to codex also". They do not on natedev (see What exists today).

**Goal:** a Codex seat whose `apply_patch` leaves a function over the limit reads the same reason as its tool output, on natedev and the Mac, through the Phase 2 script and command string.

**Spec:**
- `post-tool-use-fn-length.py` accepts `tool_name: "apply_patch"`: the files are the patch's `*** Add File: <path>` and `*** Update File: <path>` headers, an `*** Update File:` followed by `*** Move to: <new>` checking `<new>`; `*** Delete File:` is skipped. Relative paths resolve against the payload's `cwd`. The `.rs` filter, verdict, output and log are Phase 2's, with `"agent": "codex"`. The reason is the whole message Codex shows the model, so it keeps `The edit was applied.`
- `codex_hooks.py` (typed, standard library; `CODEX_BIN` when set, else `codex` on `PATH`, else `~/.local/bin/codex`; `CODEX_HOME` when set, else `~/.codex`):
  - The one hook: event `PostToolUse`, matcher `apply_patch`, handler `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py\"", "timeout": 10}`. The command string is byte-identical to the Phase 2 `settings.json` entry, so the trust hash is the same on both machines.
  - `install`: read `<CODEX_HOME>/hooks.json` (absent → `{"hooks": {}}`; invalid JSON → exit 1 naming the file, which stays untouched). If no `PostToolUse` group holds a handler with this command, append a new group `{"matcher": "apply_patch", "hooks": [<handler>]}` at the end of `hooks.PostToolUse`, so existing groups keep their indexes and their recorded trust (the Mac's file has PostToolUse groups 0 and 1, trusted); write with 2-space indent to a temp file in the same directory, then `os.replace`. Then trust it: start `codex app-server` (stdio, the default transport) with cwd `$HOME`; `initialize` as `codex_mesh.py` does (`clientInfo.name` `"codex_hooks"`); `hooks/list` with `{"cwds": [<HOME>]}`; take the entry whose handler command equals this command and whose `sourcePath` is `<CODEX_HOME>/hooks.json`; if `trustStatus` is `untrusted` or `modified`, `config/batchWrite` with `{"edits": [{"keyPath": "hooks.state", "value": {<key>: {"trusted_hash": <currentHash>}}, "mergeStrategy": "upsert"}], "reloadUserConfig": true}`; list again. Exit 0 printing `trusted <key>` only when the entry reads `trusted` and `enabled: true`; otherwise exit 1 naming the status, or the RPC error (a `config/batchWrite` refusal reports the server's message, which names any invalid config key). The whole exchange is bounded at 30 s and the server is killed on exit.
  - `check`: the same list without writing; exit 0 when trusted and enabled, else exit 1 with `fn-length codex hook: <absent|untrusted|modified|disabled>`.
- `codex_mesh.py` is unchanged: seats read user-layer hooks with no override.

**Files:**
- `scripts/hooks/post-tool-use-fn-length.py` — `apply_patch` payloads.
- `scripts/hooks/codex_hooks.py` — new: `install`, `check`.
- `scripts/hooks/test_fn_length.py` — `apply_patch` cases.
- `scripts/hooks/test_codex_hooks.py` — new: tests with a stub app-server.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/codex_hooks.py`.
- `test` — `scripts/hooks/test_fn_length.py` (the `apply_patch` cases) and `scripts/hooks/test_codex_hooks.py`, from the Spec alone:
  - `apply_patch` payloads: an Update File over the limit blocks with `"agent": "codex"`; an Add File relative to `cwd`; Update plus Move to checks the new path; Delete File only prints nothing; a patch touching only `.md` prints nothing;
  - `codex_hooks.py` against a stub `codex` executable placed alone on `PATH` that answers stdio JSON-RPC from a state file (initialize, `hooks/list` built from the temp `hooks.json` and recorded trust, `config/batchWrite` recording the edit): a fresh install creates `hooks.json` with one group and trusts it; a two-group file shaped like the Mac's gains the group at index 2 with groups 0 and 1 unchanged; a second install adds no group and sends no `config/batchWrite`; a `modified` entry is trusted again; `check` exits 0 and 1 by status; a stub that exits at once makes `install` exit 1 with a message; invalid `hooks.json` exits 1 and the file is byte-identical after.

**Constraints from prior phases:**
- Phase 2 built `fn_length_lib.long_functions(rs_file) -> tuple[LintScope, list[FunctionLength]]`, the hook entry and its block JSON, the log at `FN_LENGTH_HOOK_STATE` or `~/.local/state/fn-length-hook/blocks.jsonl`, and the `settings.json` command string this phase repeats byte for byte.
- Tests never run the real `codex`, never write `~/.codex`, and set `CODEX_HOME`, `HOME` and `FN_LENGTH_HOOK_STATE` to temporary directories.

**Acceptance gate:**
- From the worktree root, both `unittest discover` lines from the Delegation Context green; basedpyright 0 errors and 0 warnings on every changed `.py` file.
- Install (unit director, after the merge reaches `~/.claude` main and the Mac has pulled): `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/codex_hooks.py" install`, then `check`, on natedev (`dangerouslyDisableSandbox`), and on the Mac over `ssh mac` printing `rc=$?` inside the command (Mac ssh always exits 0). Both print `trusted …`; the Mac's existing PostToolUse groups 0 and 1 and its SessionStart entry are unchanged. Record that time as T_codex in PDT.
- End-to-end smoke (natedev): in a scratchpad crate denying `pedantic`, `codex exec --skip-git-repo-check -C <crate> "<ask for one function of 101 statement lines added with apply_patch>"` (`dangerouslyDisableSandbox`); the run's output shows the reason and `blocks.jsonl` gains an `"agent": "codex"` line. Delete the crate.

### Phase 4 — Re-measure four days after both hooks are live · status: todo

#### Work Order

**Blocked by:** G1 — 96 hours after T_codex (Phase 3's As-built). The window must hold no hours from before T_codex.

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Goal:** the plan states whether the hook cut too_many_lines failures at the source, against Phase 1's baseline and a stated threshold, with proof that both hooks fired.

**Spec:**
- Confirm the four script hashes: `sha256sum` in `~/.local/state/nightly-review/2026-10-06/work/lints/` gives `c2de20870ab2de9b5438e469c565d25c48185438ab42dd77807d817655b5d4fa cost.py`, `a775765b96311a783c8d6af8cdc925071b89b4ac423ae8ffa75b88fe81bf9abf loop.py`, `e178642d9bb1690562c132d824256198cc8f6e622b779b94ca76abbc75e841ce clippy_fail_lints.py`, `18754c36c8c545928a082e3bded49c688f029221b917b0f1e6c164028e4b6522 tml_lengths.py`; a mismatch stops the phase. Run, in the background with `set -o pipefail`, from `~/.local/state/nightly-review/2026-10-06/work/lints/`: `python3 cost.py 4 too_many_lines`, `python3 loop.py 4 too_many_lines`, `python3 clippy_fail_lints.py 4`, `python3 tml_lengths.py 4`, and the totals query `select count(*), sum(status!=0), min(started_at) from steps where repo='hana' and step='clippy' and started_at>=datetime('now','-4 days')` against `~/.local/state/buildlog/index.sqlite` opened read-only (`?mode=ro`), between T_codex + 96 h and T_codex + 100 h, so the 4-day window starts after T_codex.
- Derive the same three numbers as Phase 1: too_many_lines-including failed steps (`clippy_fail_lints.py`) per 100 hana clippy steps; sole-cause failed steps (`cost.py`) per 100; sole-cause seat time per day = (`loop.py` failed-call wall + repair-gap sum) / span days, the span running from the window's first hana clippy step to the run.
- **Success** when all three hold: too_many_lines-including failed steps per 100 hana clippy steps ≤ 25% of Phase 1's rate (13.3 per 100, so ≤ 3.3); sole-cause seat time per day ≤ 25% of Phase 1's (1.07 h, so ≤ 0.27 h); and the control — `blocks.jsonl` holds at least one `"agent": "claude"` and one `"agent": "codex"` line inside the window, counting natedev's file and the Mac's read over `ssh mac`. A failed control means a hook did not fire, and the rates say nothing about the hook.
- Residuals: for each too_many_lines diagnostic in the window's failed steps (file and line from the step log), say whether a block for that file precedes it in `blocks.jsonl` (the hook fired; the agent linted before splitting) or none does (an edit the hook did not see: a shell edit, rustfmt growth past the limit, or a person). Report the two counts and the five most frequent files.
- Write the numbers, the verdict and the residual counts into this phase's As-built, and send the showrunner the verdict line.

**Files:**
- `docs/plans/build-followups-fn-length-hook.md` — this phase's As-built (the closeout writes it).

**Seats:** 1 writer — `impl` runs the commands and reports; nothing splits and there is no code or test lane.

**Constraints from prior phases:**
- Phase 1's As-built holds the baseline: 13.3 too_many_lines failed steps per 100 hana clippy steps, 4.9 sole-cause per 100, 1.07 h sole-cause seat time a day (run 2026-10-06 08:17 PDT, span 3.96 days). Phase 2's As-built holds T_claude, Phase 3's T_codex.
- The scripts and the buildlog are read-only (user, 2026-10-06).
- Saved run output stays under a few GB: read each run and delete it before the next.

**Acceptance gate:**
- All hashes match, every command exits 0, and the As-built states each of the three success conditions as met or not, with its number.
