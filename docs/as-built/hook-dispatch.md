# Hook dispatch: one Python process per hook event

**Status:** as-built (build-followups-efficiency phase 4, 2026-10-28).

## What it does

Claude Code starts one process per registered hook command. Before this
change, a Bash call started 6 Python interpreters for PreToolUse and 2 for
PostToolUse, and a Stop started 4. Each interpreter pays its start-up and
import cost again.

`scripts/hooks/hook_dispatch.py <event>` runs all the Python hooks of one event
in one interpreter. The events are `pre_tool_use`, `post_tool_use` and `stop`.
It reads stdin once, then runs each hook file in table order. The decisions,
the text to Claude and the user, and the exit codes stay the same as with
separate hooks.

## The tables

Each row is a hook file in `scripts/hooks/`, the tool-name matcher it had in
`settings.json`, and its timeout (600 s unless stated). The order is the
`settings.json` order before the change.

| Event | Hooks, in order | Matcher |
| --- | --- | --- |
| `pre_tool_use` | heredoc-guard, tool-output-read, showrunner-move-guard, change-review-gate, checkpoint-doc-gate | `Bash` |
| | scratch-guard (10 s) | `Bash\|Write\|NotebookEdit` |
| `post_tool_use` | basedpyright, fn-length | `Edit\|MultiEdit\|Write` |
| | context-usage, banned-words | all tools |
| `stop` | direct-continue, conversation-pause, prose-banned-words, herdr_tab_title | — |

`settings.json` has one entry per event in place of those hooks:

- PreToolUse: matcher `Bash|Write|NotebookEdit`, timeout 3040 s.
- PostToolUse: no matcher, timeout 2430 s.
- Stop: no matcher, timeout 2430 s.

Each outer timeout is the sum of the event's hook budgets plus 30 s of
dispatcher overhead, so every hook keeps its full budget.

`scripts/hooks/test_hook_dispatch.py` checks that the tables match
`settings.json`: a dispatched hook file must no longer appear as an entry of
its own.

These hooks stay outside the dispatcher, as their own entries:

- the shell hooks: `pre-tool-use-build-hold-guard.sh` (Bash),
  `berth_pre_edit.sh`, `berth_post_bash.sh` and `pre-tool-use-brp-launch-gate.sh`;
- `pre-tool-use-push-gate.py` (PushNotification, 120 s);
- the SessionStart, SubagentStart, PreCompact and UserPromptSubmit hooks.

The random-ack PostToolUse entry is removed (user, 2026-10-28 11:59 EDT).
`post-tool-use-random-ack.sh` stays. Its header names the `settings.json` entry
that turns it on again.

## How a hook runs

- Each hook file is loaded as a fresh `__main__` module from its path, through
  importlib's bytecode cache. Its streams (with `stdin.buffer`), file
  descriptors, argv, environment, cwd, import path and signal handlers are
  isolated. `SystemExit` maps to the hook's exit code.
- A hook file still runs as a script, so its tests and any caller that runs it
  by path keep working.
- A crash, exception or timeout in one hook never stops the hooks after it.
  A timed-out hook gives no decision and no output, the same as a timed-out
  hook process.
- A hook's own `SIGALRM` budget is multiplexed with its outer deadline.
- Library modules stay imported between hooks. That is the start-up saving.

## How the results combine

The full rules are in the module docstring. In short:

- Any exit 2 wins. stdout is dropped, and stderr is the stderr of the exit-2
  hooks in table order, joined by a blank line.
- Otherwise the dispatcher exits 0 and merges the JSON of the hooks that
  exited 0:
  - `permissionDecision` by precedence deny > defer > ask > allow, with the
    reasons of the winning hooks joined;
  - every `additionalContext` joined in table order;
  - `systemMessage` texts joined;
  - any `continue:false` wins, with its `stopReason`;
  - a PostToolUse or Stop `decision:block` wins, with the reasons joined.
- A hook that exits with another code, or prints invalid JSON, adds a
  `systemMessage` line that names the hook and its stderr.
- Where the Claude Code docs state no rule (simultaneous stderr, reasons,
  `updatedInput`, unknown keys), the docstring writes down the policy.

## Bench

Child CPU per hook event, measured by `bench_hooks.py`: 20 runs per side,
`settings.json` before and after, the same payloads and transcript.
natedev, 2026-10-28 20:51–20:53 EDT. Load average was 150–230 (other builds
ran), so the absolute numbers are high, but both sides ran in the same
window.

| Event | Before | After | Change |
| --- | --- | --- | --- |
| PreToolUse, one Bash call | 419.1 ms (7 commands) | 218.5 ms (2 commands) | −48% |
| PostToolUse, one Bash call | 157.4 ms (4 commands) | 132.2 ms (2 commands) | −16% |
| **One Bash call** | **576.5 ms** | **350.7 ms** | **−39%** |
| Stop | 317.4 ms (4 commands) | 168.3 ms (1 command) | −47% |

The PostToolUse "before" column includes the random-ack hook (10.5 ms), which
this change removes. An earlier run at a lower load (same day) gave
PostToolUse 143.8 → 104.2 ms and Stop 229.3 → 134.1 ms.
