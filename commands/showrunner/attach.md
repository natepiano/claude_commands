---
description: Attach or detach child showrunners.
argument-hint: "<child> [parent] | detach <child> | tree"
---

# Attach

**Usage:** `/showrunner:attach <child> [parent]`, `/showrunner:attach detach <child>`,
`/showrunner:attach tree`. Use each showrunner row's staff name for the child and parent.
Omit the name for the caller's own production.

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunners.py" <verb> …`
with the sandbox off, and print its output as it comes:

- `attach <child> [<parent>]` stores that the child reports to the parent. With
  no parent it uses the production this session runs. It tells both showrunners
  in one line each, and says when one of them is not running so you can tell it
  later.
- `detach <child>` removes that. The child is a root again: no showrunner
  above it shows its items for the user.
- `tree` prints every showrunner on this machine, each child indented under its
  parent, with its session name now or `<not running>`.

`attach` refuses and names the cause when:

- the child or the parent has no update timer, so no showrunner runs it;
- the child would report to itself;
- the child already reports to a showrunner: detach it first;
- the parent already reports to the child, directly or through others;
- a running child or parent has a session name that is not its production's
  staff name: rename the showrunner first (`/showrunner:rename`).

A child showrunner is never a unit. It keeps its own production doc, update
timer, merge branch and units, and its staff row is a showrunner row.
What each side does once attached is in `/showrunner:produce` → Nested
showrunners.

The tree is one machine: a showrunner on the Mac cannot attach to one on
natedev. A production that wraps drops out of the tree, because an attachment
counts only while both ends have an update timer.

When the command exits non-zero, report its error in one line.
