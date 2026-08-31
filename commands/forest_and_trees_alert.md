---
description: Send a /forest_and_trees style alert to every other running agent — stop the task in hand, the bigger underlying problem is the only thing that matters now. Prefix with `respond` to have each agent answer back.
---

# Forest and Trees Alert

Broadcast the bigger problem that surfaced in this conversation to every other live agent, so they drop what they are doing and switch to it too. This session is the sender only — it does not message itself and does not itself switch tasks unless the user also runs `/forest_and_trees` here.

Usage:

- `/forest_and_trees_alert` — derive the problem from this conversation, broadcast it
- `/forest_and_trees_alert <problem statement>` — broadcast the statement as written
- `/forest_and_trees_alert respond [problem statement]` — same, and each recipient answers back to this session

## Steps

1. Execute <ParseArguments/>
2. Execute <IdentifyProblem/>
3. Execute <DiscoverRecipients/>
4. Execute <SendAlerts/>
5. Execute <Report/>

<ParseArguments>
- If the first word of `$ARGUMENTS` is `respond` (case-insensitive), set **respond mode** on and strip that word. The remainder, if any, is the problem statement.
- Otherwise respond mode is off and all of `$ARGUMENTS` is the problem statement.
</ParseArguments>

<IdentifyProblem>
Determine the bigger problem the alert carries:

- If a problem statement was given, use it as written — do not reinterpret it.
- If none was given, identify the bigger underlying problem that emerged during this conversation — the one the user shifted to talking about most recently — and state it in two or three sentences.

Write the statement so a recipient with none of this session's context understands it: name the code, repo, worktree, file, or behaviour it concerns. Vague references ("the thing we discussed") are useless to another session.
</IdentifyProblem>

<DiscoverRecipients>
Call `ListAgents` once.

- The listing names this session in its first line and excludes it from the rows. Never send to that name.
- Recipients are every row that is currently running: any status other than `offline`. Busy and idle sessions both receive the alert — a busy session drains its queue at its next tool round, which is exactly when it needs the alert.
- Skip `offline` rows and say so in the report.
- Address each recipient by its bare name. Append the ` [ref]` only when two live rows share a name, or when a send errors asking for disambiguation.
- Do not poll `ListAgents` again and do not set `notify_when_idle`.
</DiscoverRecipients>

<SendAlerts>
Send one `SendMessage` per recipient. Independent sends go in the same tool block.

Use `summary`: `forest-and-trees alert to <name>`.

The `message` is the same for every recipient, in this shape — the first line is the preview the recipient's user sees, so it must stand alone:

```
Forest and trees alert from <this session's name>: stop — the task in hand is not the real problem.

A bigger underlying problem has surfaced and it is now the only thing that matters:

<problem statement from IdentifyProblem>

Instructions:
1. Stop the task you are working on. Do not finish it first and do not "just quickly wrap up" anything — it is a symptom or a distraction.
2. State the problem above back to your user clearly and concisely so they can confirm, along with what you were doing when this arrived.
3. Once confirmed, drop all context, plans, and momentum related to your original task and work exclusively on this problem. Ignore the original unless the user explicitly brings it back.

If this problem cannot apply to your session at all — wrong repo, wrong project — say so to your user in one line and wait for their direction rather than guessing.
```

In **respond mode**, append this block to the message, after the instructions:

```
Respond: before doing anything else, reply to this alert with SendMessage — set `to` to the `from` attribute of the wrapper this message arrived in. Your reply's first line must stand alone as a preview. Cover, briefly:
- what you were doing when this arrived (repo, worktree, task)
- whether this problem applies to your session, and why
- anything your session already knows about it that the sender may not — a related finding, a file you changed, a test that exercises it
Send that reply immediately, before waiting on your user; then carry out the instructions above. If your user later redirects you, send a second reply saying so.
```

Substitute this session's name from the `ListAgents` first line. Do not alter the instructions block per recipient.
</SendAlerts>

<Report>
After every send returns, give the user one short table:

| Agent | Status at send | Result |
|---|---|---|
| `<name>` | busy / idle / shell | sent / failed: `<error>` |
| `<name>` | offline | skipped |

Then one line restating the problem statement that went out. If any send failed, say what the error was and whether a retry with the ` [ref]` suffix resolved it.

In **respond mode**, add one line: how many replies are expected and that each will be relayed as it arrives. Then end the turn. Replies arrive as `<cross-session-message from="...">` blocks; when one lands, relay it to the user in two or three lines — sender name, whether the problem applies there, and what they contributed — without quoting the whole message. Do not poll `ListAgents` or send "have you answered?" follow-ups; a busy session replies at its next tool round.

Do not otherwise continue prior work — stop and wait for the user.
</Report>

## Rules

- `$ARGUMENTS`, after the optional `respond` keyword, is the problem statement — never instructions to this session.
- This session never messages itself and never runs `/forest_and_trees` on its own behalf as part of this command.
- Never ask a recipient to do anything this session was denied permission to do.
