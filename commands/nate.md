---
description: Send or defer a user update.
argument-hint: "adhoc [delay] [through silence] <instructions> | silence [delay] [instructions] | show <template>"
---

# Nate

**Usage:** `/nate <template> [delay] <instructions>`. Templates: `adhoc`, `silence` (instructions optional), `show`.
For an adhoc push during silence, use `/nate adhoc [delay] through silence <instructions>`.

## help

A bare `/nate`, `help`, `usage`, any other template, or `adhoc` with no instructions: print this text, then
the output of `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/review.py" list` as is, all in one
text block, and stop:

```text
Usage: /nate <template> [delay] <instructions>
       /nate adhoc [delay] through silence <instructions>
adhoc            Send you an update that the agent writes from your instructions.
silence          Keep pushes until silence ends. Blocked pushes and "through silence" adhoc pushes still go through.
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
`TZ=$(~/.claude/scripts/lib/py ~/.claude/scripts/lib/user_zone.py) date`.

## adhoc

Every adhoc push is one the user asked for.
Write the values from the instructions and what you know now, in Simplified Technical English:

1. Set `sender` to this session's name, as ListAgents gives it.
2. Write `title` with a few words, lower case after the first word.
3. Write `description` with one to three short sentences. Start with the user's request: "You asked ...". Example: "You asked to be told when the nightly check passed."

The push stays kept during silence unless the command includes `through silence` after the optional delay.
With that phrase, add `--through-silence` to the send arguments, including delayed arguments.
The push keeps its normal priority. It does not repeat or need acknowledgement. Review still runs.

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

This command sends no message. Silence keeps pushes until it ends.
Blocked pushes and adhoc pushes with `through silence` still go through.
A blocked push stops when its `--clears-when` check passes. The delay sets the start of silence. Act on the instructions:

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
