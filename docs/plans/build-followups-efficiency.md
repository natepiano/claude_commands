# efficiency

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Phase 4: one Python dispatcher per hook event runs the event's Python hooks in one interpreter, so a Bash call starts 2 Python processes instead of 8, and a Stop starts 1 instead of 4. Phase 5: the checkpoint doc gate judges the staged tree and real git command words. Phase 6: every Claude and Codex session starts inside its worktree's nix shell. Phase 7: the launch scan sees accounts.py's guard with no exemption, and the unused SessionStart nix-shell hook is removed.

> **Production: natedev** — unit `efficiency-unit`; production doc `docs/plans/natedev-production.md`

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

### Phase 4 — One Python dispatcher per hook event  · status: done

**Goal:** a Bash call runs its PreToolUse Python hooks in one Python process and its PostToolUse Python hooks in one more, and a Stop runs its Python hooks in one process, with the same decisions, the same text to Claude and the user, and the same exit codes as today.

#### As-built

- Checkpoint fad3a1f3. The full record is `docs/as-built/hook-dispatch.md`.
- `scripts/hooks/hook_dispatch.py` runs the Python hooks of PreToolUse, PostToolUse and Stop in one interpreter each, in the old `settings.json` order, with the same decisions, text and exit codes. The combination rules are in its docstring. `settings.json` has one dispatcher entry per event; the shell hooks (with origin's `pre-tool-use-build-hold-guard.sh`), push-gate and the other events stay as their own entries. The random-ack entry is removed; its script header says how to turn it on again.
- Bench (N=20, same window, natedev under load): child CPU per Bash call 576.5 -> 350.7 ms (-39%), per Stop 317.4 -> 168.3 ms (-47%).
- Gate: the full merge gate added no failure; the one red (`direct/test_progress_history`, AF_UNIX path too long) fails the same way on origin dab6cf38.
- Live check on main d76569ca (2026-10-28 23:00 EDT): scratch-guard refused a Write with its usual text, PostToolUse blocked forbidden words in a tool output, the Stop dispatcher blocked a turn, and a new probe session's prose word reached the prose log (that hook logs and never blocks).

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
- The `nix-heal` notifier timer (every 5 minutes) is registered on main. A SessionStart hook was built and then dropped in phase 7 (showrunner ruling): the timer and the PATH wrappers cover the case.

**After the merge:** the showrunner adds PATH wrappers for bare `claude`, `claude1`-`claude4`, `codex`, `codex1` and `codex2` in /etc/nixos on both machines; accounts-unit wires `accounts.py exec`. Both get the merge commit.

### Phase 7 — Launch scan sees accounts.py's nix shell guard  · status: done

**Goal:** `scripts/lib/test_claude_launch_scan.py` has no exemption, and the unused SessionStart nix-shell hook is gone.

#### As-built

- Checkpoint 4c174618. The full record is `docs/as-built/nix-shell-launch.md` (Launch scan, Timer registration).
- The scan counts a same-module guard function as a guard: a top-level function that calls `ensure` on `nix_env` or on a value from `import_module("...nix_env")`, or that calls another guard by bare name (fixed point). Nested functions, methods and other-module attribute calls do not count. The `accounts/accounts.py` exemption is removed; its `launch_shell_environment` is seen as a guard.
- The unregistered SessionStart hook `scripts/hooks/session-start-nix-shell.py`, its test, and the `heal.py --wait-idle` path only it used are removed (showrunner ruling, 2026-10-28: the five-minute nix-heal timer and the PATH wrappers cover the case; revisit only if a failed build from such a session is seen). `heal.py sweep --only <id> --check` stays for the `nix_heal.failed` push.
- Checks: 41 tests pass, basedpyright clean. The full merge gate was stopped by a natedev build hold; the showrunner gates at merge.
