---
description: Send the user a targeted update from a named template, now or after a delay; the agent fills in the details.
argument-hint: "adhoc [delay] <message instructions>"
---

# Nate

**Usage:** `/nate <template> [delay] <message instructions>`. Templates: `adhoc`. Any other template, or no
instructions: print the usage line and stop.

A delay comes after the template name, in the user's plain words: `in ten minutes`, `in an hour`,
`at 3pm`, `tomorrow at 9`. No delay: send now.

Write the values from the instructions and what you know now, in Simplified Technical English:

- `sender`: this session's name, as ListAgents gives it.
- `title`: a few words, lower case after the first word.
- `description`: one to three short sentences: what happened and what it means for the user.

The send.py arguments:

```sh
--to user --from "<sender>" --need note --text-id nate.<template> \
  --value "sender=<sender>" --value "title=<title>" --value "description=<description>"
```

- Now: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" <arguments>`.
- Delayed: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send_later.py" --in <span> -- <arguments>`,
  with a systemd span (`10min`, `1h`), or `--at "YYYY-MM-DD HH:MM"` in America/New_York from `date`.

Report the result in one line; a delayed one names the send time in EDT.

The user receives:

```text
Title: <sender> <title>
Description: <description>

<template>
```
