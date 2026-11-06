---
description: Start a child production showrunner.
argument-hint: "<showrunner name> [--doc <path>] [resume <session id> <dir> [account <label>|config-dir <dir>]] [check]"
---

# Add showrunner

**Usage:** `/showrunner:add_showrunner <showrunner name> [--doc <path>]`, with `resume <session
id> <dir>` to bring back a lower showrunner whose session ended, or `check` to
see whether it would start.

Run this in the showrunner session of a running production. It starts the
showrunner of the production the doc describes, and that production then
reports to yours. Start a lower showrunner only this way, never in your own
pane and never as a unit. User, 2026-10-12: "top level (L1) showrunners should
make sure to use tmux to start lower level showrunners / showrunners should
still be grouped in the same accounts".
With the user's 2026-10-13 move to herdr, an L1 showrunner uses
`add_showrunner.py` to open each lower showrunner in its own herdr session.

Run, with the sandbox off:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_showrunner.py" --production <showrunner name> [--doc <path>] --model <your model id> --effort <your effort level> [--resume <session id> --cwd <dir> [--account <label>|--config-dir <dir>]] [--check]
```

Pass `--doc <path>` only when the showrunner has no staff row yet.
A resume or a rerun reads the doc from the existing row.

Pass your own model id and effort level, so the lower showrunner runs as you
do. Your system prompt names your model's exact id. When you cannot tell your
effort level, ask the user which to use. Show the script's output. If it exits
nonzero, show its message and stop.
On launch it prints `started <showrunner name>: herdr session attach agents, workspace <showrunner name>, tab <showrunner name>`: every
Claude session is a tab of the one herdr session, `agents`.

What it does:

- It starts `claude` with the showrunner staff name in the production's showrunner checkout.
  It reads the doc from `--doc` for creation, or from the staff row for an existing showrunner.
  It runs `/showrunner:produce <doc>`. With `resume` it
  resumes that session in `<dir>`, the directory it ran in, with
  `/showrunner:produce <doc> resume`.
- New and resumed sessions start on the configured showrunners account. An
  `--account` or `--config-dir` naming another account is refused; with the
  showrunners account not configured, it refuses and names
  `accounts.py move showrunners <claudeN>`. It carries no unit
  mark, so it is never relayed to as one of your units.
- It waits up to three minutes for the new showrunner to register its update
  timer, then attaches it to your production and tells it so. Session records
  that cannot be read for a moment are read again until the three minutes
  end. A resumed
  showrunner that already reports to you is left as it is.
- `check` runs every refusal below and starts nothing.

It refuses, and starts nothing, when:

- the doc is not a production doc;
- that production already has a running showrunner. A timer left by a session
  that ended does not count: that is what `resume` is for;
- a herdr session with that name is already live;
- the production is your own, or one you report to, directly or through
  others;
- this session runs no production;
- herdr or the session records cannot be read;
- Claude is not trusted, on the selected account, in the folder the session
  starts in: the doc's checkout, or the folder `resume` names. A trusted
  parent folder does not count; the folder, its repository, or the main
  checkout of a worktree must be trusted. The user trusts it first, by
  running Claude on that account there once and accepting. A showrunner never
  answers a trust prompt (live check, 2026-10-13: a child sat on it for the whole wait).

If the session starts and no showrunner appears in time, the command says so,
exits 1 and leaves the session running. Look at it in its tab (the line above names it); once it has its update timer, run `/showrunner:attach <showrunner name>`.

If Claude opens but is not ready, such as at a dialog, the command prints its
screen, sends no prompt, exits 3 and leaves the session open. Look at it in its tab.

The user reaches the lower showrunner in its tab of `agents` or by
Remote Control under the same name. `/showrunner:rename` changes the staff name and the live session names.
What each side does once attached is in
`/showrunner:produce` → Nested showrunners. To take it out of the tree, use
`/showrunner:attach detach <showrunner name>`; wrapping its production does the same.
