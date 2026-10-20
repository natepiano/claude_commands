---
description: Send the user a targeted update from a named template, now or after a delay; the agent fills in the details. `silence` keeps pushes until it ends.
argument-hint: "adhoc|silence [delay] <instructions>"
---

# Nate

**Usage:** `/nate <template> [delay] <instructions>`. Templates: `adhoc`, `silence` (instructions optional).
Any other template, or `adhoc` with no instructions: print the usage line and stop.

A delay comes after the template name, in the user's plain words: `in ten minutes`, `in an hour`,
`at 3pm`, `tomorrow at 9`. No delay: now. Delayed commands go through
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send_later.py" (--in <span> | --at <time>) [--run silence] -- <arguments>`,
with a systemd span (`10min`, `1h`), or `--at "YYYY-MM-DD HH:MM"` in America/New_York from `date`.

## adhoc

Write the values from the instructions and what you know now, in Simplified Technical English:

- `sender`: this session's name, as ListAgents gives it.
- `title`: a few words, lower case after the first word.
- `description`: one to three short sentences: what happened and what it means for the user.

The send.py arguments:

```sh
--to user --from "<sender>" --need note --text-id nate.<template> \
  --value "sender=<sender>" --value "title=<title>" --value "description=<description>"
```

Now: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" <arguments>`. Report the
result in one line; a delayed one names the send time in EDT.

The user receives:

```text
Title: <sender> <title>
Description: <description>

<template>
```

## silence

Sends no message. While silence is on, pushes are kept and go out when it ends; blocked (priority 2)
pushes always go through. The delay says when silence starts. Act on the instructions:

- None: `on` (lasts until `off`).
- An end (`until 7am`): `on --until "YYYY-MM-DD HH:MM"`; a length (`for an hour`): `on --for 1h`.
- `off` or `status`: that subcommand.
- `auto <length>` (`auto 45min`), `auto off`, or `auto` alone (shows it): run
  `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/auto_silence.py" [<span>|off]`, never delayed.
  After that long with no prompt in a showrunner or unit director, silence starts and the user gets one
  push; their next prompt there ends it, with one push. A silence set by hand never ends this way.

Now: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/silence.py" <subcommand>`. Delayed:
send_later.py with `--run silence -- <subcommand>`. Report one line with the start and end in EDT.
