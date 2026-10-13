---
description: Start a lower production's showrunner in its own tmux session, on the showrunner account, already reporting to this one.
argument-hint: "<production doc> [resume <session id> <dir>] [check]"
---

# Add showrunner

**Usage:** `/showrunner:add_showrunner <production doc>`, with `resume <session
id> <dir>` to bring back a lower showrunner whose session ended, or `check` to
see whether it would start.

Run this in the showrunner session of a running production. It starts the
showrunner of the production the doc describes, and that production then
reports to yours. Start a lower showrunner only this way, never in your own
pane and never as a unit. User, 2026-10-12: "top level (L1) showrunners should
make sure to use tmux to start lower level showrunners / showrunners should
still be grouped in the same accounts".

Run, with the sandbox off:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_showrunner.py" --production <PRODUCTION_DOC> --model <your model id> --effort <your effort level> [--resume <session id> --cwd <dir>] [--check]
```

Pass your own model id and effort level, so the lower showrunner runs as you
do. Your system prompt names your model's exact id. When you cannot tell your
effort level, ask the user which to use. Show the script's output. If it exits
nonzero, show its message and stop.

What it does:

- It starts `claude` in a detached tmux session named for the production's
  slug (the doc's file name without `-production.md`), in that production's
  showrunner checkout, with `/showrunner:produce <doc>`. With `resume` it
  resumes that session in `<dir>`, the directory it ran in, with
  `/showrunner:produce <doc> resume`.
- The session is on the showrunner account at any depth: it takes the main
  login, whatever account your units use. It carries no unit mark, so it is
  never relayed to as one of your units.
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
- a tmux session with that name is already live;
- the production is your own, or one you report to, directly or through
  others;
- this session runs no production;
- there is no tmux, or the session records cannot be read;
- Claude is not trusted, on the showrunner account, in the folder the session
  starts in: the doc's checkout, or the folder `resume` names. A trusted
  parent folder does not count; the folder, its repository, or the main
  checkout of a worktree must be trusted. The user trusts it first, by
  running `claude` there once and accepting. A showrunner never answers a
  trust prompt (live check, 2026-10-13: a child sat on it for the whole wait).

If the session starts and no showrunner appears in time, the command says so,
exits 1 and leaves the session running. Look at it with `tmux attach -t
<slug>`; once it has its update timer, run `/showrunner:attach <slug>`.

The user reaches the lower showrunner with `tmux attach -t <slug>` or by
Remote Control under the same name. What each side does once attached is in
`/showrunner:produce` → Nested showrunners. To take it out of the tree, use
`/showrunner:attach detach <slug>`; wrapping its production does the same.
