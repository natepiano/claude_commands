# Quota alerts: the receiver's protocol

Natedev's agent-notes job checks Claude and Codex weekly readings. Its notes and quota parts have separate process limits in `config/agent_notes.toml`; quota uses the notes part's saved readings. Account notes have numbered labels, role lists, live-session counts and a `## Live sessions` table; an account is in use when it has a role or a live session.

## Claude: what happens next

`/account_moves` manages an ordered one-shot log on natedev. Say “when units reach 1% switch to claude2”, “what's set up” or “drop the units one”. A hold may be requested in minutes. The Mac reads and edits the same log through natedev. See `docs/accounts.md` for the CLI, hold semantics and chains.

At 2% weekly or below, an in-use Claude account with any uncovered group sends one phone reminder per episode. Its title is “Claude work on claude3 may stop”, its body is “claude3 has 2% of its weekly usage left, and no move is set up for the units or showrunners on it. When it runs out, they move to a Claude account with room, or stop if none has any.” and its action is “In the natedev session, type /account_moves and the move you want, for example: when units reach 1% switch to claude2.” Only entries targeting another account and backed by readable role history count as coverage; entries covering every group on the account suppress it; accounts used only by ungrouped sessions send nothing. A failed delivery retries at the next check; success never repeats within the episode. Missing readings preserve the episode; known recovery or reset closes it silently.

Claude sends no weekly session alert, 30-minute repeat, showrunner relay, `Quota restored:` notice or instruction to start no new work. `/quota_ack` and its acknowledgement state are removed. `/quota_refresh` still refreshes readings when the user reports a reset; routing restoration is recorded in the moves journal without a session notice. A scheduled move consumes its entry when its group selection is recorded, keeps the usual safe shutdown/showrunner notices, and sends no phone alert.

## Codex notices

Workers use the same one-shot log: “when workers reach 1% switch to codex2” or “when workers reach 1% switch to claude”. Workers entries have no minute hold. A move to Claude holds there until the source account's weekly reset, even if Codex has room earlier. Entries run once in log order and leave the log after a recorded move; refused swaps keep them.

Above 0% and at or below 2%, Codex sends one phone reminder per episode when there is no workers entry targeting another account and no scheduled Claude hold is open. At 0%, the default move is handled in the same pass and no reminder is sent. A delivered reminder stays delivered across zero and subsequent readings until the weekly reset. An unreadable move journal makes coverage empty. The reminder names the default destination picked now, excluding the low account, and mentions paid credits only when that account allows them. It offers a concrete `/account_moves` command to choose that destination explicitly; leaving the default needs no action. The title names the Codex account with room. It names paid credits only when credits are allowed and no other Codex account has room.

A pane worker's proved quota refusal marks its pass refused and keeps it open for redispatch after the account move.
The move preserves its native conversation, pane, and terminal.
The launcher reads the transcript before another submission.

The refusal sends one relay to the director of the stopped worker after the refusal is routed.
The relay names the outcome when it is known and tells the director not to rerun the assign command.
The pass stays open, except when no-worker mode started: then the stop of the Codex workers ends it as canceled.
The outcome is one of these: workers moved to an account, workers wait for a reset time, or no-worker mode started.
The caller supplies `--stopped <session id> <worker>`.
Unknown directors receive no message; a waiting follow keeps its continuation.

Zero, credits forecasts, Codex-to-Codex moves and unknown-account refusals write log lines without session delivery. Phone pushes, move journal rows and the natedev prompt notice remain. Restoration is recorded only in `moves.jsonl` as `routed back` or `routing not restored`; `/quota_refresh` tells nobody. Showrunners keep no held quota-alert list. The legacy configuration `repeat_minutes` is unused; `always` supplies recipients only for Claude five-hour and Claude no-room notices, alongside running showrunners.

The Codex `5-hour limit:` notice from `five_hour.py` writes one log line per low spell, with its back-above line also logged. The Claude five-hour notice is unchanged: tell the user in one line and change nothing else. Escalation goes to the phone unless the user types in a terminal within 15 minutes.

A Codex notice gives the reported credits and limit-reset count. Without an account move entry, low quota above zero switches nothing. At weekly zero, credits on moves workers to included Codex usage before paid credits. With credits off, workers may also move to another account using its own credits. A provider refusal still requests a move. The launcher reports the serving account, refusal time and known window through `agent_notes.py blocked --window weekly|five_hour|unknown --refused-at <aware ISO time>`. A later reading identifies the window only when exactly one window is at zero. Recorded refusal overrides positive saved percentages.

A known 5-hour refusal waits when its reset is in the future and less than 20 minutes away.
One log line says `workers wait for <label>'s 5-hour reset at <time>`.
Exactly 20 minutes, a passed reset, or an unknown window or reset causes a move.
The picker excludes the refusing home and chooses the Codex account with room whose weekly reset is soonest, including credits.
The workers role changes through `swap workers`.
A pane move stops a live agent, then resumes its conversation in the same pane.
An active or refused pass receives one resume message; an idle worker receives no text.
The log says `workers moved from <old> to <new>: <old> refused (<window> limit until <reset>)`.
That follow starts `Continue where you stopped.` before held messages when the new home becomes available or its wait ends.
An exited command leaves its rerun to the director.

When no other Codex account has room, the existing Claude fallback changes assignments and its notice names the earliest reset. A failed move to the picked home also opens that fallback; the notice names the failed move and its cause. Owed assignments return when the selected Codex account can work and its refusal has resolved; assignments changed by the user meanwhile stay as chosen. A refusal from an outgoing account changes no role or assignments. A refusal creates no weekly episode. The natedev session shows fallback changes on the user's next prompt.

Each Codex pass fixes one worker selection, identity and checked home's live reading with its observation time. Spare accounts raise no low alert; making one spare preserves its episode until its own quota recovers or resets. Unknown identity preserves episodes and pending holds.

Codex move advice names its account and says `workers move to <picked> when Codex refuses work`, or `no other Codex account has room until <earliest reset>`. Above zero it uses the picker for advice; at zero the account’s credits setting decides whether workers stay or move.

Codex five-hour markers belong to a home's signed-in account and suppress repeated log lines during a low spell. Cleanup still retires queued Codex relays and escalation holds from older versions when the account changes, including when the selected account has no usage window. Unknown identity leaves that state alone. Claude notices continue independently.

A proved turn refusal carries the account that served the native conversation.
An unknown refusing identity opens no fallback.
A release failure or failed reviewer preparation command is not a provider refusal.
Fallback state distinguishes no fallback, an account-owned fallback, and a legacy fallback with an unknown owner.

`~/.local/state/accounts/moves.jsonl` records each transition under an append lock.
Entries share the time, tool, subject, and stable move id.
A workers role write records `requested`; a proved destination resume records success for that worker.
Pane moves record the request, confirmation, or failure by worker staff id and move id.
The request includes the retained terminal.
Failures, waits, wait completion, no room, and renewed room have separate entries.
Readers skip malformed lines, including during duplicate confirmation checks.
Requests omit an unknown `reset`; waits carry a known reset.
Refusal state stays in `refusals.json`.
A usage store failure writes a log line without preventing alerts.
Advice reads the saved store.

`~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py burn --since <ISO> [--until <ISO>]` reports points spent per observed day for each account and the roles it served. It reads `~/.local/state/agent-notes/readings.jsonl` and `~/.local/state/accounts/roles.jsonl`; before 2026-10-13 00:00 UTC, the login in `~/.claude`'s note label identifies historical showrunner usage. From that cutover onward, readings are labeled by account alone, with no role attribution. Historical unit directory selections resolve to the note label of that directory's login before matching readings. An unreadable historical identity refuses the report; it never picks a note label without one. Role changes split the report, including when unit directors share the account in `~/.claude`. Decreases between paired readings measure spend; an increase in remaining allowance starts a new span, so a weekly refill never becomes negative spend. Window edges and role changes within an observation interval divide its spend in proportion to elapsed time. Unobserved time and the interval across a refill contribute neither spend nor observed days. One reading alone gives no rate. `--until` defaults to now; an ISO timestamp without an offset is interpreted in the user's zone, set in `~/.claude/config/user_zone.conf`. The command reads local data and launches nothing.

## Codex weekly usage at zero

`codex_credit_accounts` in `config/accounts.conf` names the Codex accounts that may spend paid credits. Every other Codex account uses only its included usage. The setting follows the account label, including a numbered account held in `~/.codex`.

At 0% weekly with a future refill, the workers' selected account opens one zero episode. The catalog text is logged once and sent to the phone. A failed phone delivery retries every pass; sessions receive no copy. The separate low reminder remains one phone push per episode; it has no session alert or repeat. A scheduled move to Claude holds until its source reset and prevents the zero policy from undoing the move. A refill, fresh positive reading or selection of another account closes the zero episode silently.

With a workers entry queued, credit notices name the queue, not a fallback; a known refusal reset waits on the current account until that reset. Retries keep the original empty-queue facts and apply queue advice only while a move is queued.

With credits on and no queued workers entry, workers move only to a Codex account with included usage in each window.
A missing 5-hour window means it has not been used.
A weekly window with usage left needs no reset time.
That move sends no alert or move note.
Without included room, workers spend their current account's paid credits.
Each pass checks for included usage; available room causes a move and closes the paid episode without an alert.
A queued move or open Claude fallback keeps control.
The notice names the balance, refill, and destination when credits run out.
With credits off, workers prefer included usage in both windows and the soonest weekly refill.
Only when no account has weekly room can they move to another account's paid credits.
The notice names that choice.
The pane move stops the live agent before it resumes the conversation.
The request records `cause: credits off`, separately from provider refusal.
Without room, or after a failed move, that pass routes Codex assignments to Claude.
The notice names the earliest refill and any failure.
The fallback remains until that refill or the selected account recovers.
Turning credits on does not end it early.
An account with credits off cannot buy room from its credit balance.

While credits are on, an hourly forecast is logged; only the first hourly forecast also goes to the phone, with phone-only retries on failure. It uses decreases in numeric balances from `agent-notes/readings.jsonl`, skips purchases and divides usage by the observed interval. Less than 30 minutes of observation or a newest reading older than 10 minutes makes usage unknown. Whole-credit rounding may report zero use; a missing observation never means zero use. Unlimited and unknown balances are stated explicitly.

The paid-credits opening notice uses `quota.paid_credits`. While a workers move is queued, every send and retry uses `quota.paid_credits_queued`: workers spend credits until the move runs, or wait if credits run out or are turned off. The saved notice keeps the ordinary values. If the queue empties before a retry, that retry uses `quota.paid_credits` again. Old saved notices are rebuilt from the current account reading. Both notices give the action to remove the account from `codex_credit_accounts`. An episode rereads this setting on every pass. The first on-to-off change requests the credits-off move once, stops forecasts and repeats no opening alert. When the workers stay on the account, that change also turns on no-worker mode and stops the Codex workers. An off-to-on change is recorded in the episode, so a later on-to-off change acts again.


Replay checks defer entries when their journal cannot be read or decoded. An undecodable final line with no trailing newline is skipped with a log naming the file; an undecodable middle line still blocks. Rows are appended as complete encoded lines. See `docs/accounts.md` for the engine guards and install order.
