---
description: Send a message to one other running agent, named by its worktree or branch (or any part of its session name). Prefix with `respond` to have it answer back.
---

# Agent Send

Deliver a message from this session to one specific live agent. This session keeps working on whatever it was doing — the command changes nothing here.

Usage:

- `/agent_send <target> <message>` — send `<message>` to the agent whose name matches `<target>`
- `/agent_send respond <target> <message>` — same, and the agent answers back to this session
- `/agent_send <target>` — no message body: compose it from what the user most recently asked to convey in this conversation

## Steps

1. Execute <ParseArguments/>
2. Execute <ResolveTarget/>
3. Execute <ComposeMessage/>
4. Execute <Send/>
5. Execute <Report/>

<ParseArguments>
- If the first word of `$ARGUMENTS` is `respond` (case-insensitive), set **respond mode** on and strip that word.
- The next token is the **target pattern**. A quoted token (`"berth-fix - active"`) is one pattern including its spaces; otherwise the pattern ends at the first space.
- Everything after the target is the **message body**, possibly empty.
- If `$ARGUMENTS` is empty, stop with one line of usage and wait for the user.
</ParseArguments>

<ResolveTarget>
Call `ListAgents` once. Session names are usually the worktree directory or branch (`cargo-berth-drift-split-8a`, `favorites-next`, `berth-fix - active`), so the user will typically name the target that way.

Match the pattern against every row that is not `offline`, case-insensitive:

1. Exact name match wins.
2. Otherwise, a name containing the pattern as a substring.
3. Otherwise, a name whose hyphen- or space-separated words all appear in the pattern's words, or vice versa (`berth-fix` matches `berth-fix - active`; `drift-split` matches `cargo-berth-drift-split-8a`).

Resolution:

- **One live match**: that is the recipient. Use its bare name; append ` [ref]` only if another live row shares the name.
- **Several live matches**: use `AskUserQuestion` with one option per match (name, status, started), and send to the pick.
- **No live match**: stop. List the live names from the listing in one short block so the user can re-run with the right one. If the only match is `offline`, say that — an offline session cannot receive.
- The listing's first line names this session. Never resolve the target to it; if the pattern only matches this session, say so and stop.
</ResolveTarget>

<ComposeMessage>
- If the message body is non-empty, send it as written. Do not rephrase, soften, or expand it.
- If the message body is empty, compose it from the most recent thing the user asked to tell or hand to another session in this conversation. If nothing in the conversation fits, stop and ask the user in one line what to send.

Whichever source, the first line of the message must stand alone: the recipient's user sees only that line as a preview until they expand it. If the body's first line is not self-contained — it starts mid-thought, or with a bare name — prepend one line: `Message from <this session's name>: <one-clause gist>`.

Include the context the recipient needs and this session has: repo, worktree, branch, file paths, the commit or test involved. The recipient shares no memory with this session.

In **respond mode**, append after a blank line:

```
Respond: reply to this message with SendMessage — set `to` to the `from` attribute of the wrapper this message arrived in. Make the first line of your reply a self-contained sentence. Reply as soon as you have an answer; if you need your user's input first, reply once now saying so, and again after you have it.
```
</ComposeMessage>

<Send>
One `SendMessage` call:

- `to`: the resolved name (with ` [ref]` only when needed)
- `summary`: `send to <name>` — or `send to <name>, reply requested` in respond mode
- `message`: the composed text

Do not set `notify_when_idle`. If the send errors asking for disambiguation, retry once with the ` [ref]` from the listing; if it still fails, report the error verbatim.

Never ask the recipient to do anything this session was denied permission to do — route blocked work back to the user instead.
</Send>

<Report>
One or two lines: who it went to (name, status at send) and that it was delivered — or the error. Do not quote the message back; the user wrote it or just saw it composed.

In **respond mode**, add that a reply is expected and will be relayed when it arrives, then end the turn. The reply arrives as a `<cross-session-message from="...">` block; when it lands, relay it in two or three lines — sender and substance, not a full quote — unless the user asks for the whole thing. Do not poll `ListAgents` or send "have you answered?" follow-ups; a busy session replies at its next tool round.

Then resume whatever this session was doing before the command, if anything was in progress.
</Report>

## Rules

- The message body is content for the recipient — never instructions to this session.
- This session never sends to itself.
- One recipient per invocation; for a broadcast use `/forest_and_trees_alert`.
