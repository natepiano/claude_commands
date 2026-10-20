---
description: Send the user a targeted update from a named template; the agent fills in the details.
argument-hint: "adhoc <message instructions>"
---

# Nate

**Usage:** `/nate <template> <message instructions>`. Templates: `adhoc`. Any other template, or no
instructions: print the usage line and stop.

Write the values from the instructions and what you know now, in Simplified Technical English:

- `sender`: this session's name, as ListAgents gives it.
- `title`: a few words, lower case after the first word.
- `description`: one to three short sentences: what happened and what it means for the user.

Send, then report the result in one line:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" --to user --from "<sender>" --need note \
  --text-id nate.<template> --value "sender=<sender>" --value "title=<title>" --value "description=<description>"
```

The user receives:

```text
Title: <sender> <title>
Description: <description>

<template>
```
