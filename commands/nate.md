---
description: Send the user a targeted update from a named template, now or after a delay; the agent fills in the details. `silence` keeps pushes until it ends; `show` prints a phone push template.
argument-hint: "adhoc|silence [delay] <instructions> | show <template>"
---

# Nate

**Usage:** `/nate <template> [delay] <instructions>`. Templates: `adhoc`, `silence` (instructions optional), `show`.

## help

A bare `/nate`, `help`, `usage`, any other template, or `adhoc` with no instructions: print this text, then
the output of `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/review.py" list` as is, all in one
text block, and stop:

```text
Usage: /nate <template> [delay] <instructions>
adhoc            Send you an update that the agent writes from your instructions.
silence          Keep pushes until silence ends. Blocked pushes still go through.
show <template>  Show a push template with its placeholders and one example.

Phone push templates:
```

## show

`/nate show <id>`: run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/review.py" show <id>`
and print its output as is, in a text block. An unknown id prints a short message and the list.

## delay

A delay comes after the template name, in the user's plain words: `in ten minutes`, `in an hour`,
`at 3pm`, `tomorrow at 9`. No delay: now. Delayed commands go through
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send_later.py" (--in <span> | --at <time>) [--run silence] -- <arguments>`,
with a systemd span (`10min`, `1h`), or `--at "YYYY-MM-DD HH:MM"` in the user's zone, from
`TZ=$(python3 ~/.claude/scripts/lib/user_zone.py) date`.

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
result in one line; a delayed one names the send time in the user's zone, with its label.

The push is reviewed before it is sent; the user may receive the reviewer's rewrite. If review holds
it (pushover exit 6, `pushover: not sent: …`), report in one line that it was not sent, with the reason.

The user receives the reviewed title and description, which start as:

```text
Title: <sender> <title>
<description>

Sent by: <sender>
Template: nate.adhoc
```

Every push ends with these `Sent by` and `Template` lines; a push with no text id shows `Template: none (free text)`.

## silence

Sends no message. While silence is on, pushes are kept and go out when it ends; blocked (priority 2)
pushes always go through, and each is cancelled when its `--clears-when` check passes. The delay says when silence starts. Act on the instructions:

- None: `on` (lasts until `off`).
- An end (`until 7am`): `on --until "YYYY-MM-DD HH:MM"`; a length (`for an hour`): `on --for 1h`.
- `off` or `status`: that subcommand.
- `auto on` (2 h idle), `auto <length>` (`auto 45min`), `auto off` (turns it off), or `auto` alone (shows
  it): run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/auto_silence.py" [on|<span>|off]`,
  never delayed.
  After that long with no prompt in a showrunner or unit director, silence starts and the user gets one
  push; their next prompt there ends it, with one push. A silence set by hand never ends this way.

Now: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/silence.py" <subcommand>`. Delayed:
send_later.py with `--run silence -- <subcommand>`. Report one line with the start and end in the
user's zone, with its label.
