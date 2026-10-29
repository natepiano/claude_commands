# efficiency

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Phase 4: one Python dispatcher per hook event runs the event's Python hooks in one interpreter, so a Bash call starts 2 Python processes instead of 8, and a Stop starts 1 instead of 4. Phase 5: the checkpoint doc gate judges the staged tree and real git command words. Phase 6: every Claude and Codex session starts inside its worktree's nix shell.

> **Production: build-followups** — unit `efficiency-unit`; production doc `docs/plans/build-followups-production.md`

## Source

Showrunner of build-followups, 2026-10-28 (measured by builds-unit):

> one Bash call starts 8 Python hooks and 2 shell hooks (settings.json:176 PostToolUse, :234 PreToolUse), and Stop starts 4 Python hooks each turn. That is about 0.3 s of CPU per Bash call, in every session. Build one Python dispatcher per hook event that imports the checks as modules, with the same order, the same exit codes and the same outputs. Measure CPU per Bash call before and after. The settings.json change needs the user's typed go in your pane before it is committed. Plan it, then run it with Codex workers, one checkpoint. Merge-first applies.

## Delegation Context

- **Project:** `~/.claude` config repo. The unit director names the worktree and branch git reports at dispatch in every Work Order.
- **Project started:** 2026-10-28
- **Stack:** Python 3 standard library checked by basedpyright; Claude Code command hooks registered in `settings.json`.
- **Layout:** hooks and their tests in `scripts/hooks/`; the interpreter shim `scripts/lib/py`; hook registration in `settings.json` (`hooks` key); as-built docs in `docs/as-built/`.
- **Baseline (natedev, 2026-10-28, child CPU user+sys per run of each registered command, Bash payload):** PreToolUse 357 ms (6 Python hooks: heredoc-guard 39, tool-output-read 38, showrunner-move-guard 64, change-review-gate 134, checkpoint-doc-gate 29, scratch-guard 54); PostToolUse 142 ms (berth_post_bash.sh 8, random-ack.sh 10, context-usage 59, banned-words 64); Stop 248 ms (direct-continue 57, conversation-pause 39, prose-banned-words 60, herdr_tab_title 91). About 0.5 s of CPU per Bash call and 0.25 s per Stop. The unit director's bench script feeds one payload to each command N times and reads `RUSAGE_CHILDREN`; the final numbers come from N=20 runs before and after on the same machine.
- **Claude Code hook semantics (docs, https://code.claude.com/docs/en/hooks):** matching hooks of one event run in parallel, with no documented order. Exit 0 applies the JSON on stdout; exit 2 blocks (PreToolUse: stderr is the deny reason; PostToolUse: stderr goes to Claude; Stop: stderr is the reason to continue); any other exit, or invalid JSON, is a non-blocking "hook error" notice. Plain stdout on exit 0 goes only to the debug log for these three events. PreToolUse decision precedence across hooks is `deny` > `defer` > `ask` > `allow`; every hook's `additionalContext` reaches Claude. A command hook's default timeout is 600 s; a timed-out hook gives no decision. The docs do not state how several hooks' stderr, reasons, `systemMessage`, `continue:false` or Stop/PostToolUse `decision:block` combine.

## Phases

### Phase 4 — One Python dispatcher per hook event  · status: pending

**Goal:** a Bash call runs its PreToolUse Python hooks in one Python process and its PostToolUse Python hooks in one more, and a Stop runs its Python hooks in one process, with the same decisions, the same text to Claude and the user, and the same exit codes as today.

**Scope:**
- New `scripts/hooks/hook_dispatch.py <event>` (`pre_tool_use`, `post_tool_use`, `stop`). It reads stdin once and holds one ordered table per event: each row is a hook file in `scripts/hooks/`, the tool-name matcher it has in `settings.json` today, and its timeout. Order is the `settings.json` order of today.
- It loads each hook file as a module (by path, under a unique module name), and runs its existing entry point with that payload as stdin and with stdout and stderr captured, and maps `SystemExit` to the exit code. A hook file stays runnable as a script, so its tests and any caller that runs it by path keep working. Where a hook needs a small change to run this way (a `main()` that reads `sys.stdin`, module-level side effects, an output a subprocess writes straight to file descriptor 1), change the hook in the smallest form and keep its script behavior.
- One hook's crash, exception or timeout never stops the hooks after it; the result for that hook is what Claude Code gives a failed or timed-out hook today (no decision from it). A per-hook timeout keeps today's value (scratch-guard 10 s; the others the 600 s default).
- Combine the hook results into one result with the effect that separate hooks have today:
  - any exit 2 → exit 2, and stderr is the exit-2 hooks' stderr texts in table order, joined by a blank line;
  - else, JSON from the hooks that exit 0 is merged: PreToolUse `permissionDecision` by the documented precedence, with the reasons of the hooks that gave the winning decision joined in table order; every `additionalContext` joined in table order; `systemMessage` texts joined; `continue:false` from any hook wins with its `stopReason`; PostToolUse and Stop `decision:block` from any hook wins with the reasons joined;
  - a hook that exits with another code, or prints invalid JSON, adds a line to `systemMessage` that names the hook and its stderr, the same notice a user sees today.
  - Write the exact rules, and each case where the docs state no rule, in the module docstring.
- `settings.json` (user's typed go: "go", 2026-10-28 11:50 EDT, in the efficiency-unit pane): the Python hooks for these three events move behind the dispatcher. PreToolUse: heredoc-guard, tool-output-read, showrunner-move-guard, change-review-gate, checkpoint-doc-gate (Bash) and scratch-guard (Bash|Write|NotebookEdit) become one entry with matcher `Bash|Write|NotebookEdit`. PostToolUse: basedpyright and fn-length (Edit|MultiEdit|Write), context-usage and banned-words (all tools) become one entry with no matcher. Stop: direct-continue, conversation-pause, prose-banned-words and herdr_tab_title become one entry. The PostToolUse random-ack entry is removed and not added to the dispatcher (user's typed go, 2026-10-28 11:59 EDT); `post-tool-use-random-ack.sh` and `config/acknowledgements.txt` stay, and the script header gains one line naming the `settings.json` entry that turns it on again. The other shell hooks (berth_post_bash.sh, berth_pre_edit.sh, brp-launch-gate.sh), push-gate (PushNotification, 120 s), SessionStart, SubagentStart, PreCompact and UserPromptSubmit stay as they are.
- Tests in `scripts/hooks/test_hook_dispatch.py`: for each event, a table of payloads (allow, deny from each blocking hook, ask, two hooks blocking at once, additionalContext from two hooks, a crashing hook, a timed-out hook, a non-matching tool) where the dispatcher's exit code, stdout JSON and stderr equal the combination rule applied to the separate hook processes' results. A test pins that the dispatcher table equals the hook files and matchers that `settings.json` no longer lists.

**Out of scope:** porting the shell hooks to Python; SessionStart and other events; changing what any hook checks.

**Acceptance:**
- Bench: child CPU per Bash call and per Stop, N=20 before and after on natedev, recorded in the as-built doc.
- The full merge gate `--tests-only` passes; basedpyright 0 errors and 0 warnings on `scripts/hooks/`.
- A live check in a new session: a Bash call that a gate refuses is refused with the same text; an allowed call runs; a Stop with a banned word in prose still blocks.

### Phase 5 — Checkpoint doc gate judges the staged tree and real git command words  · status: done

**Source:** showrunner of build-followups, 2026-10-28 13:30 EDT (before the phase 4 checkpoint).

**Goal:** the checkpoint doc gate refuses only a real checkpoint commit or merge whose staged tree has no doc record, and it reads commit and merge words only where git reads them.

**Scope:**
- `scripts/direct/verify.sh doc` judges the staged index: it writes the staged tree to a stable checkout (`<target>/verify-doc/<repo hash>/tree`, private index, tree marker, one flock for the run) and records that tree. The hook compares the record with the tree of the commit about to be made. A staged subset, `--only`, a pathspec and `-i` are accepted after the same check; `-p` stays refused.
- `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` reads shell words, not text: reserved words, opaque commands, `env -C`, the merge rules, git's short-option clusters for commit and merge, and `--` ends options. Its docstring states the threat model: it catches the commit forms an agent writes in ordinary use, it does not stop deliberate hiding, and where it cannot read a command that holds checkpoint text it refuses.
- Tests in `scripts/hooks/test_checkpoint_doc_gate.py` and `scripts/direct/test_verify_doc.py`; the as-built `docs/as-built/rustdoc-once-and-final-targets.md` and `commands/unit/checkpoint.md`.

**Landing:** after review round 4 the showrunner called one last bounded round (git short-option clusters, F022 and F023); further findings go to a follow-up phase.

**Acceptance:** both test modules pass; basedpyright clean; the full merge gate `--tests-only` adds no failure to the origin tip.

### Phase 6 — Every Claude and Codex session starts inside its worktree's nix shell  · status: done

**Source:** the user, via the showrunner, 2026-10-28: "it has to be rock solid reliable - never fail again"; "bare claude should be forced to use this also".

**Goal:** every start path for a Claude or Codex session loads the worktree's `.envrc` nix shell first, or refuses with one line; a session that started outside it is healed.

#### As-built

- Checkpoint 187c88b9. The full record is `docs/as-built/nix-shell-launch.md`.
- `scripts/lib/nix_env.py` (`ensure`, `exec`) is the one way a session starts: herdr's `launch_agent`, restart and pane restore, add_unit, add_showrunner, berth_fix, agent_bg, agent_exec, cli_agent, codex_mesh, review and make_a_worktree all go through it, and `scripts/lib/test_claude_launch_scan.py` fails on a start site outside it. herdr gives a new session a clean base environment (launcher variables and the parent's nix shell removed).
- `verify.sh` re-runs itself under `nix_env.py exec` when it starts outside the shell, with one line.
- `scripts/nix_env/heal.py sweep` restarts a session outside its shell, after the build-hold check, on its own account; when the restart cannot fix it, it pushes `nix_heal.failed` (texts reviewed and approved).
- Live check: `ensure` in `~/rust/bevy_brp` loaded the shell in 0.4 s; `exec` ran a program inside it.
- Open: the SessionStart hook `scripts/hooks/session-start-nix-shell.py` is in the tree, but its `settings.json` registration waits for the user's typed go. The `nix-heal` notifier timer (every 5 minutes) is registered after the merge.

**After the merge:** the showrunner adds PATH wrappers for bare `claude`, `claude1`-`claude4`, `codex`, `codex1` and `codex2` in /etc/nixos on both machines; accounts-unit wires `accounts.py exec`. Both get the merge commit.
