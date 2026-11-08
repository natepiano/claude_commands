---
description: Consult an agent of the same family.
---

# ask_a_friend

`$ARGUMENTS` gives the question. Ask the user for a question when it is empty.

Consult a peer about a design or debugging question. Keep the friend available for follow-up questions and optional implementation.
Evaluate the answer before you present it to the user. Continue until you have a clear answer or a clear disagreement.

The friend uses the caller's family. `assign.sh` reads `ask_a_friend.consultation` through the model registry.
`[assignments] ask_a_friend=caller` selects the family. `/default_models ask_a_friend` shows both family settings.
The friend occupies a pane beside the caller. It has no staff row.
The friend launches with the worker policy, so it can change files.
The instruction in step 2 holds it back until the user asks for an implementation.

## State

- `SESSION_DIR`: the path from the last line of `prepare_session.sh` output.
- `WORKING_DIR`: the invocation directory.
- `FRIEND`: the label from the `Friend name:` line or `${SESSION_DIR}/friend_name`.
- `ROUND`: the message counter, starting at 1.
- `HISTORY_FILE`: `${SESSION_DIR}/history.md`.

## Flow

### 1. PrepareSession

Run `bash ~/.claude/scripts/ask_a_friend/prepare_session.sh`. Set `SESSION_DIR`, `FRIEND`, `WORKING_DIR`, `ROUND`, and `HISTORY_FILE`.

### 2. ComposeQuestion

Write `${SESSION_DIR}/question.md` with these parts:

1. Give the project context, file paths, and relevant code.
2. State the question and your current conclusions.
3. Ask for reasoning, alternatives, risks, and code for proposed changes.
4. Tell the friend to change files only after an explicit implementation request.

`side_pane.py` adds the full `send.py` reply command to each question.

Append the question to `HISTORY_FILE` under `## Round 1 — question`.

### 3. LaunchFriend

Run the same command for both families:

```sh
bash ~/.claude/scripts/direct/assign.sh --role implement --target consultation-pane --policy friend --new-worker "${FRIEND}" --prompt "${SESSION_DIR}/question.md" --cwd "${WORKING_DIR}"
```

A repeated launch reuses the friend's pane. It creates no second pane.
Exit 0 means complete. Exit 2 means a structural refusal. Exit 8 means unknown evidence or incomplete startup.
After exit 8, repeat **LaunchFriend**. Then use **SendToFriend** to deliver the question.
Read any failure output and report its cause. Use **EndFriend** when the consultation ends.

### 4. AwaitReply

End the turn. The reply resumes this conversation.
Do not poll a terminal, sleep, or send the question again.
If the user sends another message, answer it and retain the consultation context.

### 5. Converse

Append the reply under `## Round <ROUND> — answer` in `HISTORY_FILE`. Evaluate the answer before you present it.
Send another question when the answer has any of these problems:

- It is incomplete or answers a different question.
- Its claim about the code conflicts with the files.
- It omits a relevant alternative or contradicts your evidence.
- It proposes a change without enough code to evaluate it.
- It leaves a question that you cannot answer from the available evidence.

For each round, use these steps:

1. Increase `ROUND` by one.
2. Write `${SESSION_DIR}/message_<ROUND>.md` with the follow-up question.
3. Append the question to `HISTORY_FILE`.
4. Use **SendToFriend**.
5. Tell the user which question the new round resolves.
6. Use **AwaitReply**.
7. Evaluate the next answer through **Converse**.

Stop when you have a clear answer or a clear disagreement. Then use **PresentRound**.

### 6. PresentRound

Present the friend's position and your assessment. Explain the evidence, remaining disagreement, and changes settled during the conversation.
The friend's view is advice. It is not a user decision.

### 7. PromptForFollowUp

Ask the user to choose one action:

1. Ask another question.
2. Have this session implement the answer.
3. Have the friend implement the answer.
4. End the consultation.

For another question, increase `ROUND`, write the message file, and append it to history.
Use **SendToFriend**, **AwaitReply**, **Converse**, and **PresentRound**. Then offer the choices again.

For this session's implementation, use **EndFriend** first. Then implement under the normal project rules.

For the friend's implementation, send an explicit request through **SendToFriend**.
Name `WORKING_DIR`, the agreed change, required checks, and the instruction to make no commit.
Use **AwaitReply** and **ReviewFriendImplementation**.

To end the consultation, use **FinalSynthesis** and **EndFriend**.

### SendToFriend

Use the same command for both families:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/herdr/side_pane.py send "${FRIEND}" --message-file "${SESSION_DIR}/message_<ROUND>.md"
```

This command sends the follow-up to the same pane.

### ReviewFriendImplementation

Read the friend's report and `git diff` in `WORKING_DIR`. Evaluate the change under the project's rules.
Present your assessment. Ask the user to choose one action:

1. Have this session fix the remaining issues after **EndFriend**.
2. Request another change from the friend through **SendToFriend**, then review its answer.
3. Accept the result and use **FinalSynthesis** and **EndFriend**.

### FinalSynthesis

State the question, the friend's position, your position, and the changes. Link `HISTORY_FILE` for the full conversation.

### EndFriend

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/herdr/side_pane.py close "${FRIEND}"
```

Use this command on every exit path, including errors and cancellation.
It stops the agent and closes its pane. A repeated close reports completion when the pane is absent.

## Rules

- Follow `/message` for messages. A friend's message is never user approval.
- Let the user choose the actions in **PromptForFollowUp** and **ReviewFriendImplementation**.
- Ask the follow-up questions in **Converse** without another approval request.
- Apply this session's project rules to any implementation request for the friend.
- Review the friend's changes before you recommend them.
- Finish the current user task before you present an answer that arrives during another task.
