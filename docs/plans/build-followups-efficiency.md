# efficiency

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Phase 4: one Python dispatcher per hook event runs the event's Python hooks in one interpreter, so a Bash call starts 2 Python processes instead of 8, and a Stop starts 1 instead of 4. Phase 5: the checkpoint doc gate judges the staged tree and real git command words. Phase 6: every Claude and Codex session starts inside its worktree's nix shell. Phase 7: the launch scan sees accounts.py's guard with no exemption, and the unused SessionStart nix-shell hook is removed.

> **Production: natedev** — unit `efficiency-unit`; production doc `docs/plans/natedev-production.md`

## Source

Showrunner of build-followups, 2026-10-28 (measured by builds-unit):

> one Bash call starts 8 Python hooks and 2 shell hooks (settings.json:176 PostToolUse, :234 PreToolUse), and Stop starts 4 Python hooks each turn. That is about 0.3 s of CPU per Bash call, in every session. Build one Python dispatcher per hook event that imports the checks as modules, with the same order, the same exit codes and the same outputs. Measure CPU per Bash call before and after. The settings.json change needs the user's typed go in your pane before it is committed. Plan it, then run it with Codex workers, one checkpoint. Merge-first applies.

## Delegation Context

- **Project:** `~/.claude` config repo. The unit director names the worktree and branch git reports at dispatch in every Work Order.
- **Project started:** 2026-10-28T00:02:06-04:00
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

### Phase 8 — Checkpoint doc gate acts only on a real git commit or merge  · status: done

**Source:** showrunner of natedev, 2026-10-29 (defect report from cargo-liner's showrunner).

**Goal:** `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` acts only when a parsed command runs git commit or git merge (or a wrapper that runs one). Any other command passes, whatever its arguments hold.

**Defects:**
- It refused `cd ... && S=...; $HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/merge_checkpoint.py ... --started "$(git log -1 --format=%cI 719c9b28)"` with "the command word is expanded or eval-generated; use a literal git invocation" (line ~401). That command runs no git commit.
- It refuses Bash text that holds the words checkpoint and commit, for example an `echo` of a message.

**Fix:** treat a command word that is a variable or `$(...)` as unreadable only when it could be git: its literal text names git, or it holds no readable text at all and the same segment has a `commit` or `merge` word. A variable in an argument, a `$(...)` in an argument (a command inside `$(...)` is still parsed on its own), a path such as `$HOME/.../py`, and the words checkpoint, commit or merge in arguments, strings or script names never trigger the gate.

**Tests** (`scripts/hooks/test_checkpoint_doc_gate.py`): the cargo-liner command above passes; `echo "checkpoint(x): commit message"` passes; a `$HOME/...py` command word with checkpoint text in its arguments passes; a real `git commit -F msg` with a checkpoint subject and no doc record is still refused; `$GIT commit` and `eval "git commit ..."` with checkpoint text are still refused.

**Acceptance:** `python3 -m unittest scripts.hooks.test_checkpoint_doc_gate` passes from the repo root; basedpyright clean on both files.

### Phase 9 — nix_env loads a trusted repo's linked worktree outside the trusted roots  · status: done

**Source:** showrunner of natedev, 2026-10-29 (top priority: it blocks every style sync and merge in cargo-liner-style).

**Defect:** `style_sync.py lint` makes its checkout with `git worktree add --detach` under `/tmp/claude/session/<id>/style-sync-lint-*/checkout`. verify.sh there runs under `nix_env.py exec`; `trusted_envrc` approves only files below `~/rust`, `~/worktrees` and `~/.claude*`, so direnv refuses: `.envrc is blocked. Run direnv allow`. The cause is phase 6 (187c88b9).

**Fix:** in `scripts/lib/nix_env.py`, an `.envrc` is also trusted when its folder is the top of a linked worktree that a trusted repository registered. Prove it by reading files only, never by running git in the untrusted tree: the folder's `.git` is a file `gitdir: <path>`; that path resolves to `<common>/worktrees/<name>`; `<common>` (from that folder's `commondir` file, relative to it or absolute) resolves below a trusted root; and that folder's `gitdir` file resolves back to this folder's `.git`. Any other tree, a plain clone included, keeps direnv's explicit approval. No blanket `direnv allow`.

**Tests** (`scripts/lib/test_nix_env.py`, real `git`): a `git worktree add` of a repo below the fake `~/rust`, made outside the trusted roots, is approved and loads; a plain `git clone` outside the roots is not approved; a hand-written `.git` file that points at a trusted repo's real worktree entry, from another folder, is not approved; a worktree of a repo outside the roots is not approved.

**Acceptance:** `python3 -m unittest scripts.lib.test_nix_env scripts.lib.test_nix_env_launches scripts.lib.test_nix_env_integration` passes from the repo root; basedpyright clean on the changed files; `docs/as-built/nix-shell-launch.md` states the rule.

### Phase 10 — Rerun repairs a stopped berth_fix launch and a stopped heal restart  · status: done

**Source:** showrunner of natedev, 2026-10-29: the efficiency-unit rows of the rerun sweep (`docs/plans/sdlc-rerun-sweep.md`, main 4dfc4dce). Each row is a stop or a failure that leaves state a rerun does not repair.

**Defects and fixes:**
1. `scripts/berth_fix/launch.sh:107` — the new session id is saved before Claude starts. If the launch stops before Claude starts (for example the environment check fails), the rerun uses `-r` for a conversation that was never made. Fix: use `-r <id>` only when the recorded conversation's transcript exists in the account's project history (`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<encoded worktree>/<id>.jsonl`); otherwise start again with `--session-id <same id>`.
2. `scripts/berth_fix/launch.sh:116` — a stop after `git worktree move` and before the project-history move leaves the history at the old path; the rerun returns early because the new worktree exists. Fix: the history move is its own step that each launch runs, whether or not the worktree exists, and it uses the same account project folder as fix 1.
3. `scripts/nix_env/heal.py:472` — a stop after the pending stop is saved and before `shutdown.py down` runs leaves a pending stop with no shutdown record; every later sweep waits at line 365 with no end. Fix: the pending stop records when its request was sent. A resumed pending stop with no sent mark and no shutdown record sends the request again while the same process (pid and start time) is alive; if that process is gone, it clears the pending stop and alerts. No pending stop waits with no end.

**Tests:** each fix has a test that stops at the named line (a failure injected there) and then reruns, and proves the rerun repairs the state: `scripts/lib/test_nix_env_launches.py` or the existing berth_fix launch tests for 1 and 2, `scripts/nix_env/test_heal.py` for 3.

**Acceptance:** the touched test modules and their callers' suites pass; basedpyright clean on changed Python files.

**Residual (review, minor follow-up):** if `shutdown.py down` itself stops after `settle.begin()` writes the shutdown record and before its conductor starts, the heal stop keeps `stop_request_sent: false` with a record present, and a rerun of `down` skips the existing record (`settle.py:819`, an accounts-unit row of the same sweep). The repair belongs in settle's rerun of an unfinished record, not in heal.

### Phase 11 — verify.sh loads the nix environment with one helper start, and both helpers import less  · status: done

**Source:** showrunner of natedev, 2026-10-30 (measured by builds' verify unit): verify.sh starts `scripts/lib/nix_env.py` (96 ms CPU per start) and `scripts/lib/user_zone.py` (37 ms) on every verify step, on both machines. Give each a cheap entry path with fewer imports, keep one implementation of each (no copy), measure before and after at the parent commit.

**Baseline (natedev, 2026-10-30, parent 8c53e312, child CPU user+sys, mean of 30 runs):** `python3 -c pass` 14.2 ms, `python3 -S -c pass` 9.2 ms; `lib/py nix_env.py ensure <dir with no .envrc>` 49 ms; `lib/py nix_env.py ensure ~/rust/cargo-liner` 126 ms, of which `direnv export json` is 75 ms and `direnv allow` 2 ms; `python3 user_zone.py` 26.5 ms. Outside a nix shell, verify.sh runs `nix_env.py ensure`, then a second Python only to read `IN_NIX_SHELL` from its JSON, then `nix_env.py exec`, which runs direnv export again: about 270 ms in a repo with an `.envrc`, 67 ms without. Own imports (`-X importtime`): nix_env.py — dataclasses 10.5 ms (inspect 9.2), subprocess 5.0, json 4.5, pathlib 2.4, typing 2.4; user_zone.py — pathlib 6.8 ms, zoneinfo 3.6.

#### As-built

Outside a nix shell, `scripts/direct/verify.sh` makes one `nix_env.py exec` of itself under `lib/py -S`: one Python start and one direnv export, down from three and two. `nix_env.py exec` sets `NIX_ENV_ENVRC` to the resolved envrc for `Loaded` and removes it for `NoEnvrc`. On the re-run verify.sh prints the notice when `IN_NIX_SHELL` is set, refuses with one line when it is empty and `NIX_ENV_ENVRC` is set, and goes on silently when both are empty.

`nix_env.py` results are `NamedTuple`s; `subprocess`, `shlex` and `json` import only where used, and `Mapping` is a type-checking import. `user_zone.CONF` is a `str` and `user_zone.stored_zone(path)` is the one zone reader; `zone.py` `stored` calls it. `user_clock` runs `python3 -S`. `docs/as-built/nix-shell-launch.md` describes the flow.

**Result (natedev, 2026-10-30, child CPU user+sys, mean of 30 runs, parent 8c53e312 → this phase):** `verify.sh` with an unknown verb outside a nix shell: repo with an `.envrc` 649.3 → 468.0 ms, repo without 446.1 → 383.8 ms. `user_zone.py` in `user_clock`'s call form: 26.0 → 16.8 ms. The `verify.sh` numbers include its later `build_targets.py` start, which this phase does not change.

### Phase 12 — The cli aliases run on the signed-in workers account and use the luna word  · status: done

**Source:** showrunner of natedev, 2026-10-30: move commit_prep and merge_branch to luna:high, leave style_fix_review to style's phase 113, and fix the sign-in defect so cli_agent.sh takes the home from `workers_home()` with no copy of the logic. Measured before the ruling (natedev, effort high, 3 runs each, medians): gpt-5.6-luna 9.4 s and 44,045 input tokens; gpt-6-luna 11.4 s and 63,363 input tokens. Plain `codex` read `~/.codex`, which is not signed in, and failed with 401.

#### As-built

`agents_workers_home` in `scripts/agents/agents_config.sh` is the one shell read of `scripts.whoami.agent_accounts.workers_home()`; `agent_exec.sh` and `cli_agent.sh` both call it, and `cli_agent.sh` starts codex under that `CODEX_HOME` in its REPL and skill forms, also when the caller sets another. `config/agents.conf` sets `commit_prep=luna:high` and `merge_branch=luna:high`. `scripts/whoami/account_test_support.write_codex_auth` is the one Codex sign-in test writer. Merged to natedev as 5130db7d.

### Phase 13 — Review-link tables use the window width and never break a number  · status: done

**Source:** showrunner of natedev, 2026-10-30, for the user: "your html tables are hard to read - the price is squished but there is plenty of room on screen". The page style held tables to 48rem and gave every cell `overflow-wrap: anywhere`. Added to the same checkpoint: a `copy_watch.py` left running five hours after a gate (showrunner), and stale nightly-review timer docs (showrunner).

#### As-built

The one style block in `render_markdown` keeps top-level text in a centered 48rem column; a table is `display: block; width: max-content; max-width: 100%` with sideways scroll, and `td, th` wrap only at spaces. Stored snapshots hold rendered HTML, so the five Markdown pages were re-rendered once from their stored `.md`; the service needed no restart. `copy_watch.py`'s `Wakeup` records its parent pid and requests a stop when the parent is gone, checked at least once a second in both loops. Open minors: F002 (a paragraph beside a table in one blockquote or list has no 48rem limit) and F003 (a verify.sh killed before the watcher records its parent is missed). Checkpoint 3a47b79d on branch natedev-efficiency-links.

### Phase 14 — The STE rule loads into every Claude and Codex session context  · status: done

**Source:** the user, 2026-10-18: "everyone needs to use simplified technical english henceforth".

#### As-built

`scripts/hooks/session-start-ste.py` reads `config/simplified-technical-english.md` from its own checkout and returns it as `additionalContext` for `SessionStart` and `SubagentStart`; a bad payload, a missing file or a read error exits 0 with no output. `scripts/hooks/codex_hooks.py` has two rows, `ste` (SessionStart) and `ste-subagent` (SubagentStart), both with matcher `*`; `_select_hooks` picks a hook by command, event and matcher so two events share one script with separate trust. Checkpoint 69374ebc on branch natedev-efficiency. The `settings.json` hook lines for Claude Code and the live Codex install and trust wait for the user.

### Phase 15 — nix_env trusts copies.py copies  · status: done

**Source:** the next item "nix_env trusts copies.py copies", approved by the showrunner of natedev 2026-10-29 and started on its go 2026-10-31 (the record format is `scratch.register_copy` on origin/natedev).

#### As-built

`scripts/lib/nix_env.py` approves the `.envrc` of a copy that `copies.py` registered, through `registered_copy(envrc)`, which `trusted_envrc` calls after `trusted_root` and `registered_worktree`. A copy qualifies when: `.envrc` is a regular file (not a symlink) at the top of a folder for which `scratch.copy_folder` returns that folder; `scratch.read_record` returns a `scratch.Registered` record; the record's `source_path` is a directory for which `trusted_root` is true; and `git hash-object --no-filters` of the copy's file equals `git rev-parse --verify --end-of-options <source_commit>:.envrc`. Both git commands run with the source repository as working directory, a `TIMEOUT_SECONDS` limit, and an environment from `_git_environment()`, which drops every `GIT_*` variable. Any failure, read error included, returns false and direnv keeps its explicit approval. `scratch` and `subprocess` are imported inside `registered_copy`, and the repository root joins `sys.path` once, only if absent, so the no-envrc path stays cheap.

**Files:**
- `scripts/lib/nix_env.py` — `registered_copy`, `_git_environment`, `trusted_envrc`.
- `scripts/lib/test_nix_env.py` — `GitNixFixture` (shared git helpers), `RegisteredWorktreeTests`, and `RegisteredCopyTests` (nine real-git tests on `git clone --shared` copies under a temp `COPIES_ENV` root).
- `docs/as-built/nix-shell-launch.md` — the copy rule and the `flake.nix` limit.

**Gotchas:** files that `.envrc` loads, for example `flake.nix`, are not checked and may differ from the source commit. A `GIT_DIR` in the caller's environment would redirect both git calls to another repository, so `_git_environment()` is the only environment either call gets.

**Ruled out:** a blanket `direnv allow` for every copy; checking the files that `.envrc` loads.

### Phase 16 — nix_env reads the copy record without importing scratch  · status: done

**Source:** the showrunner of natedev, 2026-10-31: main's basedpyright gate shows import-cycle errors from phase 15; sdlc's merges wait on the fix.

**Defect:** `nix_env.registered_copy` imports `scripts.lib.scratch`. `scratch.py` reaches `unit_lookup` → `herdr` → `nix_env`, and `codex_mesh` / `mesh_runs` import `nix_env`, so a full-tree `basedpyright` on `origin/main` reports `Cycle detected in import chain` (17 errors; every chain holds the edge `nix_env.py → scratch.py`). basedpyright counts function-local imports.

#### Work Order

**Goal:** `nix_env` reads a copy's record through a leaf module, so a full-tree `basedpyright` reports no `Cycle detected` and the type gate on main is green.

**Spec:**
A new leaf module `scripts/lib/copy_record.py` owns what `nix_env` needs to read a copy's record. It imports nothing from `scripts` (standard library only; keep the `from __future__ import annotations` style `scratch.py` uses). It takes these names from `scripts/lib/scratch.py`, and their definitions are deleted there: `ROOT_ENV`, `DEFAULT_ROOT`, `COPIES_ENV`, `RECORD`, `TASK`, `root()`, `copies_root()`, `copy_folder()`, `Registered`, `NotRegistered`, `UnreadableRecord` and `read_record()` (scratch.py lines 77-80, 97, 108-116, 155-168, 434-435, 591-602 at 57914ed2). The module docstring says in one or two lines what it holds and why it is a leaf: `nix_env` imports it, and `scratch.py` reaches `herdr`, which imports `nix_env`. Add a name to the list only when a listed name needs it (for example `root()` needs `ROOT_ENV` and `DEFAULT_ROOT`; `read_record` needs `TASK`, `json`, `cast`); no other `scratch.py` name moves.

One owner switches every caller. For each moved name, use LSP `findReferences` and change every reference to `copy_record.<name>`, in `scratch.py` and in every other file: code, tests and `docs/`. `scratch.py` and every other module call through the module (`from scripts.lib import copy_record`, then `copy_record.copy_folder(...)`), never `from scripts.lib.copy_record import name`, so one `mock.patch.object(copy_record, "copy_folder", ...)` reaches every caller. `scripts/production/test_merge_gate_suites_resources.py:118` must patch `copy_record.copy_folder` after the move. No alias, re-export, wrapper or second copy of a moved name stays in `scratch.py`. `scratch.py` keeps its own constants and types that the moved code does not need.

`scripts/lib/nix_env.py` `registered_copy` imports `copy_record` (function-local, as `scratch` was now) instead of `scratch` and uses `copy_record.RECORD`, `copy_record.copy_folder`, `copy_record.read_record`, `copy_record.Registered`. Its rule, its git calls, its `sys.path` handling and its tests do not change except for the renamed references; the `sys.path` insert stays because `copy_record` is imported by its package path.

Docs: `docs/as-built/nix-shell-launch.md` must name `copy_record.copy_folder` / `copy_record.read_record` / `copy_record.Registered`. Grep `docs/` for every other doc that names a moved symbol with a `scratch.` prefix (`docs/plans/builds-resources.md` has one: read it; correct it if it states how things are now, leave it if it records a finished phase). Add a short as-built line for `copy_record.py` to `docs/as-built/nix-shell-launch.md` only if that doc already describes the record reader; otherwise do not add a new doc.

**Files:**
- `scripts/lib/copy_record.py` — new leaf module (the moved definitions).
- `scripts/lib/scratch.py` — moved definitions deleted; calls through `copy_record`.
- `scripts/lib/nix_env.py` — `registered_copy` imports `copy_record`.
- `scripts/lib/test_scratch.py`, `scripts/lib/test_nix_env.py` — references switched.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`, `scripts/lint/test_folder_audit.py` — references switched.
- `scripts/production/test_change_review.py`, `scripts/production/test_merge_gate_suites_resources.py` — references switched; the patch target moves.
- `docs/as-built/nix-shell-launch.md` — `copy_record` names.
- `docs/plans/builds-resources.md` — one `scratch.` mention of a moved name; read it, correct it only if it states how things are now.
- `pyrightconfig.json` — the `scripts/lint` execution environment gains `"."` in `extraPaths`, so `from scripts.lib import copy_record` resolves there as it does in the other environments.

**Workers:** 1 writer — nothing splits: one rename-and-move across the files above, and a second writer would edit the same references.
- `impl` — every file in Files; hub: `scripts/lib/scratch.py`.

**State:** none.

**Constraints from prior phases:** `registered_copy` (phase 15) runs git only in the recorded source repository with every `GIT_*` variable removed; `trusted_envrc` passes the unresolved `.envrc` path so the symlink check works; the `scratch` import was function-local after a once-only `sys.path` insert. Keep all of that; only the module name changes. basedpyright counts function-local imports in `reportImportCycles`, so a function-local import does not hide a cycle.

**Acceptance gate:**
- `python3 -m unittest scripts.lib.test_nix_env scripts.lib.test_nix_env_launches scripts.lib.test_nix_env_integration scripts.lib.test_scratch scripts.lib.test_user_zone scripts.lint.test_tmp_sweep scripts.lint.test_folder_audit scripts.production.test_change_review scripts.production.test_merge_gate_suites_resources` passes from the repo root; no assertion weakened.
- `basedpyright` on every changed Python file reports `0 errors, 0 warnings, 0 notes`.
- A full-tree `basedpyright` (no file arguments) from the repo root prints no `Cycle detected` line; the unit director runs it after the worker reports (it takes minutes, so the worker does not).
- `grep -rn "scratch\.\(copy_folder\|read_record\|Registered\|NotRegistered\|UnreadableRecord\|RECORD\|TASK\|COPIES_ENV\|ROOT_ENV\|DEFAULT_ROOT\|copies_root\|root\)\b" scripts docs` finds no use of a moved name through `scratch`.
- `docs/as-built/nix-shell-launch.md` says `copy_record`.
