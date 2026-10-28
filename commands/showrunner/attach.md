---
description: Put one showrunner under another, or take it out again - the lower one then reports to the higher one; each one still shows the user only its own production's items (`/next all-below` adds those below).
argument-hint: "<child> [parent] | detach <child> | tree"
---

# Attach

**Usage:** `/showrunner:attach <child> [parent]`, `/showrunner:attach detach <child>`,
`/showrunner:attach tree`. A child and a parent are named by production slug
(the production doc's file name without `-production.md`) or, for the caller's
own production, left out. A former slug, from the doc's **Formerly** line
after `/showrunner:rename`, names the same production.

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
- the parent already reports to the child, directly or through others.

A child showrunner is never a unit. It keeps its own production doc, update
timer, merge branch and units, and gets no row in the parent's Units table.
What each side does once attached is in `/showrunner:produce` → Nested
showrunners.

The tree is one machine: a showrunner on the Mac cannot attach to one on
natedev. A production that wraps drops out of the tree, because an attachment
counts only while both ends have an update timer.

When the command exits non-zero, report its error in one line.
