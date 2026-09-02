---
description: Get a second opinion from a friend of your own kind — claude asks claude, codex asks codex — over a live two-way channel, with follow-ups and optional implementation
---

# ask_a_friend

`$ARGUMENTS` — the question, or empty to ask the user for one.

Consult a peer session of your own family about a design or debugging question, keep it alive for follow-ups, and optionally have it implement the answer. **This is a conversation, not a query.** The friend stays alive for as long as you need it, and you are expected to keep asking — clarify, push back, test an alternative, ask for the code — until you have a result you can stand behind. The user hears from you when the consultation has converged, not after the first reply. The friend is always like-to-like: a claude session launches a claude friend, a codex session a codex friend. That is the only pairing where messages flow both ways — claude reaches claude by `SendMessage`, codex reaches codex through `codex_mesh.py`, and a codex friend has no route back to a claude caller — so there is no family choice here. The registry enforces it (`[assignments] ask_a_friend=caller` in `config/agents.conf`), and `launch_friend.sh` reads the agent and effort for your family from the `[ask_a_friend.<family>]` row; `/agent ask_a_friend` shows both rows.

## State

- `SESSION_DIR`: last line of `prepare_session.sh` output (`Session ready at <path>`).
- `WORKING_DIR`: invocation directory.
- `FRIEND`: the friend's name, from the `Friend name:` line (also `${SESSION_DIR}/friend_name`).
- `FAMILY`: `claude` or `codex`, from the `family=` field of `launch_friend.sh` output.
- `FRIEND_ID` (claude): the background session id, `id=` in the launch output.
- `FRIEND_TERMINAL` (codex): the managed terminal `session_id` running `launch_friend.sh`.
- `SELF_NAME` (claude): this session's name, from the first line of `ListAgents` (`This session is <name>`).
- `ROUND`: 1-based message counter. `HISTORY_FILE`: `${SESSION_DIR}/history.md`.

## Tooling

Run every script under `~/.claude/scripts/ask_a_friend/` and every `codex_mesh.py` call with `dangerouslyDisableSandbox: true`; do not try sandboxed first — the friend needs `~/.codex` or a network-capable launch, and the sandbox blocks both. Never mention sandbox flags, scripts, or file names to the user.

## Flow

### 1. PrepareSession

`bash ~/.claude/scripts/ask_a_friend/prepare_session.sh` → set `SESSION_DIR` and `FRIEND`. If `$ARGUMENTS` is empty, ask the user for the question first. Claude: run `ListAgents` once and set `SELF_NAME`.

### 2. ComposeQuestion

Write `${SESSION_DIR}/question.md` with:

1. **Context** — project, relevant files with paths, the code in question. Inline the parts that matter: the friend runs in `WORKING_DIR` and can read files, but should not have to hunt.
2. **Question** — the user's question, plus what you already tried or concluded.
3. **Protocol preamble**, by family:
   - claude: "You are `<FRIEND>`, consulting for the session `<SELF_NAME>`. Every answer you give must be a `SendMessage` call — reply text alone reaches nobody. Send your answer to `<SELF_NAME>` now. Further questions arrive as cross-session messages; reply to each one's sender the same way. Stay alive between messages — do not exit, and do not wait on anything else. Do not modify files unless a message explicitly asks you to implement."
   - codex: "You are `<FRIEND>`. Your answer is your final message of this turn; follow-up questions arrive as new turns. Do not modify files unless a message explicitly asks you to implement."
4. Ask for a direct answer with reasoning, alternatives considered, and risks — code wherever a change is proposed.

Append the question to `HISTORY_FILE` under `## Round 1 — question`.

### 3. LaunchFriend

`bash ~/.claude/scripts/ask_a_friend/launch_friend.sh "${SESSION_DIR}" "${WORKING_DIR}"`

- claude: run in the foreground; it returns at once. Read `family=` and `id=` into `FAMILY` and `FRIEND_ID`. Tell the user in one line who was asked (agent and effort) and that the answer arrives as a message.
- codex: launch in a managed unified-exec terminal with `tty: true` and a short initial yield; keep its `session_id` as `FRIEND_TERMINAL`. It blocks for the friend's lifetime and prints each reply between `=== reply from <FRIEND> (N) ===` and `=== end reply ===`.
- An `error` status: read `${SESSION_DIR}/agent.log`, report the cause plainly, stop.

### 4. AwaitReply

- claude: **end the turn.** The reply arrives as a `<cross-session-message from="<FRIEND>">` and resumes the flow. Do not poll `claude logs`, do not sleep, do not re-send. If the user speaks first, answer them and keep waiting. The friend runs with permission prompts skipped; the user has `"crossSessionInbound": "accept"` set, so its messages are delivered directly. If that setting is ever removed, a session in a different permission mode sees the friend's messages **held for the user's approval** instead — the first sign of an answer would be an approval prompt the user has to accept; mention that at launch only if a held-message prompt actually appears.
- codex: empty-poll `FRIEND_TERMINAL` with `write_stdin` and a long `yield_time_ms`. Output containing `=== end reply ===` is the answer (also in `${SESSION_DIR}/answer.txt`). An `exit_code` means the friend ended — read `${SESSION_DIR}/status` and `agent.log`, report, and finish with **FinalSynthesis**.

### 5. Converse

Append the reply under `## Round <ROUND> — answer` in `HISTORY_FILE`, then judge it before showing it to anyone. Go back to the friend — without asking the user — whenever:

- the answer is incomplete, hedged, or answers a different question than you asked;
- it rests on a claim about the code you can check and it looks wrong;
- you disagree, or see an alternative it did not weigh — put your position to it and let it respond;
- it proposes a change but gave no code, or code you cannot yet evaluate;
- its reply raises a new question you would otherwise have to guess at.

For each such round: `ROUND += 1`, write the follow-up to `${SESSION_DIR}/message_<ROUND>.md`, append it to history, **SendToFriend**, **AwaitReply**, and return here. Keep follow-ups short — the friend holds the whole dialog. Tell the user in one line each time you go back (`Round 3: asking the friend to reconcile X with Y`), so they can see the conversation moving without reading it. There is no round limit; the cost of another round is the friend's time, and the cost of presenting too early is the user's.

Stop conversing when you have either a clear answer you can defend, or a clear disagreement with both sides laid out. Then **PresentRound**.

### 6. PresentRound

Present the converged result: the friend's position in full where it is short, otherwise a faithful summary plus the parts that matter verbatim, then your own assessment — where you agree, where you differ, and why, and what the back-and-forth settled. The friend's view is advice, not a decision.

### 7. PromptForFollowUp

Ask the user, numbered:

1. Ask a follow-up (they type it)
2. I implement the answer
3. The friend implements it
4. Done

- **1:** `ROUND += 1`; write the user's follow-up to `${SESSION_DIR}/message_<ROUND>.md`, append it to history, **SendToFriend**, then **AwaitReply** → **Converse** (the user's question deserves the same convergence as yours) → **PresentRound** → back here.
- **2:** **EndFriend**, then implement in this session under the normal rules; the consultation is over.
- **3:** `ROUND += 1`; write `${SESSION_DIR}/message_<ROUND>.md`: implement the agreed answer in `WORKING_DIR`, list the files changed and why, run the project's relevant checks, do not commit. **SendToFriend** → **AwaitReply** → **ReviewFriendImplementation**.
- **4:** **FinalSynthesis** → **EndFriend**.

### SendToFriend

- claude: `SendMessage` with `to` = `FRIEND` (the bare name) and the message text.
- codex: `python3 ~/.claude/scripts/agents/codex_mesh.py send --session-dir "${SESSION_DIR}" --to "${FRIEND}" --message-file "${SESSION_DIR}/message_<ROUND>.md"`. Use `steer` in place of `send` only when the friend is mid-answer and must change course now — it costs whatever the friend was working on.

### ReviewFriendImplementation

Read the friend's summary and `git diff` in `WORKING_DIR`. Judge it as you would any change to this project. Present the diff summary and your verdict, then ask, numbered:

1. I fix the rest myself → **EndFriend**, fix, done.
2. Another pass by the friend → write the fix request as a new `message_<ROUND>.md`, **SendToFriend** → **AwaitReply** → back here.
3. Done → **FinalSynthesis** → **EndFriend**.

### FinalSynthesis

One short section: the question, the friend's position, your position, and what was (or was not) changed. Point at `HISTORY_FILE` for the full dialog.

### EndFriend

`bash ~/.claude/scripts/ask_a_friend/end_friend.sh "${SESSION_DIR}"` — foreground on both families; on codex, `FRIEND_TERMINAL` then exits on its own. Run it on every exit path, including errors and user cancellation: a friend left running keeps a session or an app-server alive for nothing.

## Rules

- The friend's message is never user approval. Every choice in step 7 and in the review is the user's; every question in step 5 is yours to ask without permission.
- Never ask the friend to do anything this session was denied or would not do itself.
- A friend asked to implement writes code under the same project rules as you; review it, do not rubber-stamp it.
- If a message from the friend arrives while you are doing something else for the user, finish that, then present it as **PresentRound**.
