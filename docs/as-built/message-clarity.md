# Message clarity

`send.py` resolves staff names and addresses through one staff snapshot. Staff selects the row's `current_session`.
Unit names match their staff names exactly. Ambiguous staff names stop delivery.
A non-staff name uses a unique live roster session, locally before the peer.
A missing name exits 2 with a named note. It creates no relay or queue.
Explicit non-staff addresses retain their transport and queue behavior. Worker staff lookup uses native IDs from either machine's roster.
`--codex --session-dir DIR --to SLOT` remains a run-scoped worker selection.

## What it is

The user wanted every message sent to them to be checked for clarity by an agent that speaks for them. This feature does that. A written message guide sets the rules for pushes, questions and replies. Sessions load the guide with `/message_style` before they write to the user. Before any phone push is posted, a headless reviewer reads it cold against the guide. The reviewer can approve it, rewrite it, ask for missing facts or withhold it. A push that fails review is not sent, and the sender is told to revise it. Claude's built-in `PushNotification` is gated so it cannot bypass the review. Every fixed script push is a catalog entry that has been approved ahead of time. When a showrunner asks the user a question in a reply, the question is registered and reviewed first; no hook checks the reply (user, 2026-10-19). A nightly `messages` pass measures how messages did and proposes new guide rules.

## How it works

### Sender identity

`send.py` reads the sender's native ID through `roster.sender_identity(os.environ)` once per send.
A Codex thread ID takes precedence over an inherited Claude director hint.
Claude identity comes from the runtime registry checks.
`--from` remains the display and reply name. It does not select the native ID.

Every agent message includes `Sender native id: session:<id>`, `Sender native id: codex:<id>`, or `Sender native id: unavailable`.
The generated metadata forms one block directly after the preview line.
Only the `Authority:` line in that block grants authority.
Quoted labels elsewhere grant no authority.
Only an exact match with `MORTY_NATIVE_ID` in `config/send.conf` adds `Authority: morty (speaks for the user)`.
The live key must contain a valid native address. A missing or invalid key grants no mark.
A different runtime that uses `--from morty` gets a stderr note. Delivery continues without the mark.
The mark proves runtime attribution, not authentication.
Code under the same operating-system account can set its environment.

`Message.stamp` stores a known Claude sender, a known Codex sender, or an unavailable sender.
Queue records and message log records store its address in `sender_native_id`.
SSH forwarding preserves the origin stamp. The receiving shell does not replace it with its own identity.
`--forwarded-sender-stamp` requires `--here` and an environment with `SSH_CONNECTION`.
This context check prevents accidental use. It does not prove that `remote()` supplied the stamp.
A session can inherit `SSH_CONNECTION`. Code under the same account can also set it.
Queue recovery preserves the stored stamp. An old record without the field has an unavailable sender.
A drain never attributes an old message to its own runtime.
Delivered caller lines that start with `Authority:` or `Sender native id:` get a `> ` prefix.
Rendering first converts CR, CRLF, and Unicode line separators to LF.
This rule covers the summary, body, sender name, and forwarded display text.
The prefix also applies after leading spaces. Generated stamp lines keep their original form.
Queue records and logs keep the raw caller text.
These fields do not change the recipient, reply name, or message key.

### Replies to relay sockets

The headless relay uses the session name `relay/<sender>`. Session lookups identify a relay only when its saved sender and process start identity match the live process; on Linux the boot ID must also match. A real session with the same name form, or a reused PID, stays visible. Its message body names the real sender in `Shown sender:`. The name rule and its readers are described in [session-roster.md](session-roster.md). Before the headless relay starts Claude, it atomically publishes the process lifetime and sender in `message/relay_senders/<pid>.identity`, then the plain sender name in `message/relay_senders/<pid>`. The plain sender remains available after the process exits for reply recovery. It then execs Claude with that same PID, which owns `cc-socks/<pid>.sock`. A reply to a missing socket, or one whose delivery connect returns `ENOENT` or `ECONNREFUSED`, checks the saved name through staff. A staff sender uses its current session. A non-staff sender uses the live registry. Send and drain share this rule; a live socket takes only its delivery connect. Without a saved name or a live session, `send.py` reports `FAILED` (exit 3), names the address and sender on stderr, and keeps no raw socket queue. A queued message uses the session ID found at the queue decision, even if that session then exits.

`send.py drain-sockets` recovers old raw `uds__*.sock.jsonl` queues. It delivers each message in a file whose newest entry is under 24 hours old to the named live session, marked `Stuck message from <original time>:`. Recovery uses the relay record, then queued recipient metadata (`to_name`, `to-name`, `recipient_name`, or a named `to`). Each stable recovery ID gets a flushed and fsynced attempt in `message/socket-recovery-attempts.jsonl` before delivery, followed by its durable result in `message/log.jsonl`. The message log keeps one row per result; attempts do not count as messages. A rerun skips confirmed sends. An attempt without a result is archived without resending: `UNCONFIRMED: stuck reply from <time> to <name> may have arrived; archived, not resent` (exit 3); a later run has nothing to repeat. Known failed sends remain for retry, without another queued copy. Unresolved or unreadable files stay in place and are reported by path (exit 3); other files still run. Older files move intact to `message/socket-queue-archive/`; one line gives the file count and oldest and newest message times. Archive names never overwrite another item, and a retry reuses an identical uncertain entry's archive. `UNCONFIRMED` and `SENT` lines are printed and flushed before removing the queue entry; a rerun may repeat the report, without sending again. The old-file summary (`archiving <n> socket queue files: …`) is printed and flushed before any of its files move, so an interrupted move cannot hide the count; a rerun names only the files still to move.

### The message guide

- The guide is 13 rule files in `~/rust/nate_style/message/`, plus a `README.md`. The README has no `test:` line, so the loader skips it. Each rule file has frontmatter with `date_created`, `date_modified`, `tags`, `mechanism: llm`, a yes/no `test` that a cold reader answers from the message alone, and a `source` that quotes the user's words with their date. The body has `## Title`, a paragraph, `### Bad` and `### Good`.
- The tags are `all`, `push`, `question` and `reply`.

| Tag | Rule stems |
| --- | --- |
| `all` | `reader-starts-cold`, `what-happened-why-what-you-do`, `short-plain-sentences`, `before-and-after`, `times-carry-zone` |
| `push` | `title-names-the-stake`, `first-150-stand-alone`, `push-body-under-500`, `push-only-what-the-user-must-do`, `every-choice-says-what-it-does`*, `say-where-to-act`* |
| `question` | `ask-once-then-count`†, `every-choice-says-what-it-does`*, `say-where-to-act`* |
| `reply` | `ask-once-then-count`†, `explain-means-explain` |

\* tagged both `push` and `question`; † tagged both `question` and `reply`.

- A tag filter always includes `all`. That gives 11 rules for `push,all`, 8 for `question,all` and 7 for `reply,all`.
- The loader is `zsh ~/.claude/scripts/ux_style/load-ux-style.sh --guide <dir> [--tags t1,t2] [--checklist]`, the same loader the UX guide uses. It prints each rule body, then a `=== UX_CHECKLIST ===` block with one `stem — title — test` line per rule. The header name only matters for display.
- `commands/message_style.md` runs the loader on the whole guide and confirms the rule count. `/alert_user`, `/message`, `/message_review` and the `<Notify>` section of `commands/showrunner/produce.md` load it before their first message to the user in a session.
- Push form:
  - The first line is `Action: <what the user does>` or `No action needed.`, with an optional `Session: <name>` line above it. Every order rule in the guide applies after these lines.
  - The title is under 45 characters, names what is at stake and has no fixed prefix.
  - The first 150 characters make sense on their own.
  - The header plus body is under 500 characters.
  - The two approved decision templates can use up to 1024 characters.
- `ask-once-then-count` limits only the end-of-reply reminder, never an answer the user asked for.

### Push data flow

```
session ──send.py --to user --text-id ID──┐
script ──push_texts.user_send_argv → send.py --to user --text-id ID --value k=v──┤
nixos ──pushover.py --text-id ID --value k=v──┤
                                              ▼
pushover.py send(): parse → template admission → action checks (refuse = exit 2) → legacy hold lookup (free text and agent-written ids)
  → approved registered text, except agent-written ids: no review | review_off: no review | else review.review(draft)
  → agent-written nate.adhoc is checked like free text: apply rewrites; failed review means exit 6
  → print "pushover: reviewed JSON: {…}"
  → not sent (exit 6) | post via deliver() → post_message() adds Sent by / Template → pushover.jsonl
                                              ▼
send.py user(): map exit code → SENT / HELD / REPLACED / REFUSED / FAILED
  → on FAILED or failed-review HELD under a Claude session: write failed/<session_id>.json
                                              ▼
```

The fallback gate requires a reviewed blocked push whose delivery failed.
Its marker contains a registered template id and the full rendered text.
The text contains the title, action, body, and sender/template footer.
The gate permits only an exact match. A missing template id or failed gate check denies the fallback.

### The reviewer: `scripts/notify/review.py`

- `review(draft: Draft, revision: bool, *, runner=run_process, limit=math.inf) -> ReviewOutcome`. `limit` caps the seconds of the whole review, the check of a rewrite included; a review that runs out of time gives `Unreviewed("timeout")`.
- `Draft(source, priority, title, action: UserAction, body, answerable, session: NotificationSession = NO_SESSION_LINE)`.
- `ReviewOutcome = Approved | Rewritten | Questions | Unreviewed | Failed | Withheld`:
  - `Approved(draft)`
  - `Rewritten(version, failed_rules, …, failures)`
  - `Questions(best_version, questions, failed_rules, …)`
  - `Unreviewed(why)`
  - `Failed(best_version, failed_rules, …, failures, questions)`: `questions` are the reviewer's, when it asked instead of rewriting
  - `Withheld(best_version, why, …)`

  Each outcome also carries `ReviewMetadata(seconds, account, model, binary)`. Each failure is a `RuleFailure(rule, quote)`.
- Each review runs `claude -p` once, through `_review()`:
  - **Binary:** `reviewer_binary()` takes the first executable from `shutil.which("claude")`, `~/.local/bin/claude`, `/etc/profiles/per-user/$USER/bin/claude` or `/run/current-system/sw/bin/claude`. If none is found, the result is `Unreviewed("no claude binary: <paths tried>")`. The `review.jsonl` line records the resolved path or the paths tried.
  - **Account:** `reviewer_environment()` removes the caller's `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `CLAUDE_CONFIG_DIR`, then applies `accounts.prepared_account(accounts.role_account("unit").label)`.
  - **Model:** `agents_resolve_print message_review.reviewer` sets the model, with a 2 s cap on that lookup. When it resolves nothing, the reviewer uses opus at medium effort. `config/models.conf` has no `message_review` entry today, so the fallback is what runs.
  - **System prompt:** the guide filtered by tag, followed by an instruction. Pushes use `push,all` and `reviewer_prompt.md`. Replies use `reply,all` and an inline prompt. Questions use `question,all` and an inline prompt.
  - **Zone:** the draft prompt of each review (push, reply and question) starts with `zone_line()`: `User zone: AST (America/Curacao). Today: 2026-10-25.`, from `scripts/lib/user_zone.py`. Each system prompt says times are in that zone. The reviewer never assumes a zone (2026-10-25: a reviewer refused AST times as not EDT).
  - **Flags:** `--json-schema SCHEMA --output-format json --tools "" --no-session-persistence --setting-sources ""`.
  - **Time:** the whole call shares one budget of `review_timeout_seconds()`.
- The schema returns `verdict` (`send|rewrite|ask|withhold`), `title`, `action` (string or null), `body`, `questions` (at most 3) and `failed_rules` (a list of `{rule, quote}`).
- `validate()` turns the JSON into an outcome:
  - It removes an `Action: ` prefix from `action`. In push mode it removes a `Session:` line from the body when that line matches the draft's session (a different session makes the result `Unreviewed`). It also removes a first body line that repeats the action line.
  - The action form must not change. A `send` verdict must return the draft unchanged. The title must be ≤ 250 characters and the posted message ≤ 1024. `questions` are allowed only with `ask`. Any violation makes the result `Unreviewed`.
  - Code counts the length rules, `push-title-under-45` and `push-body-under-500`, against both the original and the proposed text. Any length failure the model reports is dropped and replaced by the code count. A failure whose quote is not in the original text is dropped. Each rule counts at most once.
  - Code also decides `push-one-action` (no rule file), against both the original and the proposed text: `code_failures()` adds it when `user_action.action_conflicts(action, body)` gives a reason, with the reasons as its quote. The draft prompt lists the reasons on a `Code-found second actions:` line, and `reviewer_prompt.md` tells the reviewer to remove them from the body.
  - `send` with supported failures becomes `Failed` for a push. For a reply it becomes `Unreviewed`.
  - A proposed version that fails a length rule becomes `Failed`.
  - `withhold` needs a supported failure and is allowed only in push mode.
  - `ask` becomes `Questions`. In a reply revision it becomes `Rewritten`.
- `review()` runs a second check on any rewrite. It runs `_review()`, and if that returns `Questions`, the result is `Failed`. If it returns `Rewritten`, it reviews the rewrite again with `revision=True`:
  - `Approved` keeps the `Rewritten`.
  - `Unreviewed` or `Withheld` passes through.
  - Anything else becomes `Failed(best_version)`.

  Times are summed across both passes, so wait budgets allow for two reviews.
- Any exception, of any type, becomes `Unreviewed(why)`.
- Log: every review appends a `ReviewRecord` line to `review.jsonl`. The `verdict` is `send`, `rewrite`, `ask`, `failed`, `withhold`, `unreviewed: <why>` or `review: off`. A line can also carry `failures`, `binary`, `text_id`, `detail`, `replay`, `dry`, `sample`, `kind` (`reply` or `question`), `transcript`, `reply_time` and `previous_reply`.
- Subcommands:

| Command | Does |
| --- | --- |
| `check --title T (--action A \| --no-action) --text B [--priority N]` | Dry full review (including the second check). Prints the outcome and logs `dry`. Exits 0. A `Session:` line at the start of `--text` is kept. |
| `replay [--limit N]` | Reviews sent pushes again from `pushover.jsonl`, newest first. Logs `replay`. Posts nothing. |
| `texts [--id X]… [--approve [--user-spec SOURCE]]` | Dry-reviews every catalog example. Exits 1 if any fails. See the catalog section. |
| `sample <transcripts…> [--limit 20] [--since ISO]` | Dry-reviews recent replies. See the nightly section. |
| `release --identity I -- SOURCE` | Prints one `ReleaseOutcome` word. |
| `due` | Posts expired holds (legacy, see below). |
| `switch on\|off\|status` | Creates or removes `review_off`. This is the only way to turn the review off. `/message_review` wraps it. |

### `scripts/notify/pushover.py`

- Usage: `pushover.py [--priority 0|1|2] [--source NAME] [--key KEY] [--answerable] (--action TEXT | --no-action) TITLE MESSAGE`, or `--text-id ID --value k=v…`, which takes no positional arguments and no action flags.
- Footer (user ask, 2026-10-27: an emergency push from the hana session did not say who sent it). `post_message()` is the one place a push goes out, also a push kept during silence (`post_kept()`) and a held push sent at hold expiry. It adds one last line after the review and the length checks, so a reviewed text and its approval do not change:

  ```
  <message>

  Sent by: hana · Template: auto_mode.refused
  ```

  - `Sent by` is `sender_name(source)`: the source on one line, at most 80 characters. A `send.py in <session>` source (`send.py`'s `default_sender()`) shows only the session; any other source (`stall-watch`, `rca`, `natedev`, `build-followups`, `send.py`, or the parent command line when there is no `--source`) shows as it is.
  - `Template` is the `--text-id`, or `none (free text)` for a push with no text id.
  - The footer always fits in 1024 characters: a long message is cut at the end first, never the footer.
  - An empty sender is refused: nothing is posted, the log outcome is `refused before sending: no sender`, and the exit is 2.
  - `pushover.jsonl` logs the posted message, footer included. A kept push logs its message without the footer when kept, and with it when posted.
  - Only `pushover.py` holds `api.pushover.net` under `scripts/` (`test_push_texts.py`), so no path posts round the footer.
- An emergency push (priority 2) repeats every 300 s for 3 h until the user taps Acknowledge. Deleting it on the phone does not stop it. With `--key`, the post carries `tags=push_tag(KEY)` (`key-` and the first 16 hex digits of the key's SHA-256: the same key gives the same tag). Below priority 2, or with no key, there is no tag. Priority 2 is never kept during silence, and `post_kept()` passes the kept key, so a tag is never lost.
- `pushover.py cancel [--source NAME] KEY` posts `token` to `receipts/cancel_by_tag/<tag>.json`, which stops every emergency push with the key's tag. It logs to `pushover.jsonl` with title `cancel emergency push` and outcome `cancel: cancelled N`, `cancel: nothing to cancel`, `cancel: refused: …`, `cancel: unreachable: …` or `cancel: keys missing`. It prints `pushover: cancelled N` or `pushover: nothing to cancel` and exits 0; it exits 1 when Pushover refused it or could not be reached, and 2 on usage or keys missing. No receipt is stored. (2026-10-23: a stall-watch prompt push kept repeating after the user answered the prompt.)
- While the user's silence is on (`silence.py`), `deliver()` keeps a push below priority 2 in `silence-kept.jsonl` and silence end posts it. `deliver(…, key=, text_id=)` puts `--key` and `--text-id` in the `KeptPush`, so a released push gets the same footer. A kept push with the same non-empty key from the same source replaces the older kept one (`silence.keep_here()`), so silence end sends only the newest. On the Mac `deliver()` and `cancel()` run on natedev over ssh (`pushover.py deliver` with the push JSON, `pushover.py cancel`); the Mac has no Pushover keys. When natedev cannot be reached, the Mac logs the cause and returns 1. A held push sent after its hold expires has no key (`""`), and an old kept line with no key reads as `""`. An old kept line with no text id reads as `""` and is sent as free text. `silence.py drop --source S --key K` (`silence.drop()`) removes the kept pushes with that source and non-empty key and prints how many; on the Mac it asks natedev over ssh, and gives 0 when natedev cannot answer. `send.py retire --to user` runs it, so each kept push goes out once, and the alert of a closed rca.py case is gone before silence ends. When auto silence ends (`auto_silence.come_back()`), `silence.auto_off` goes first and the kept pushes follow: title `{sender}: you are back`, body `{held}`, one of `No pushes were held while you were away.`, `The push held while you were away comes next.` or `The N pushes held while you were away come next.`
- With `--text-id`, the push is rendered from the catalog. The template sets the sender, the action and the Session line. The priority comes from the template's `need` (`note`→0, `decision`→0, `blocked`→2), unless `--priority 2` is given.
- Refused before sending (exit 2, logged as `refused before sending: <reason>`):
  - no action stated
  - both `--action` and `--no-action`
  - an empty or placeholder action (`none`, `n/a`, `nothing`, `no action`, `-`)
  - `--no-action` at priority > 0
  - an action longer than the space left in 1024 characters

  This logic lives in `user_action.py`: `parse_user_action`, `refused_at`, `FIX` and `REFUSED_BEFORE_SENDING`.
- `action_conflicts(action, body)` gives a reason for each second action in the body (the text after the `Session:` and `Action:` lines) of a push with an action. A second action is a thing to type that the Action line does not hold: `type` or `type:` and its text, or a `! <command>` shell escape (`typed_texts()`). It is also a pane or session name that is not the one the Action line names: `in the X pane`, `X session`, `session named X` (`named_places()`). `a type of`, `type the go`, `the pane` and `a Claude session` name nothing. When the Action line names no pane or session, only the first check applies. Every catalog example gives no reason (`test_user_action.py`).
- `user_action.py` also holds `SessionLine`, `NoSessionLine`, `NotificationSession`, `NO_SESSION_LINE`, `split_session`, `session_prefix` and `first_line(action, session)`. Every reader of the header keeps `Session: …` above `Action: …`.
- `push_texts.render_entry()` preserves each staff name in template values. `parse()` changes no text.
- Review order: the hold lookup runs first for free text and agent-written ids (`nate.adhoc`). Other registered ids skip it and, with a current approval (`push_texts.approval_date()`: every example's `sha256` in `push_texts_reviewed.json` matches `fingerprint()`), skip live review: outcome `Unreviewed`, detail `registered text, approved <date of the newest approval>`. Agent-written ids are checked like free text even with a current approval. Otherwise `review.review()` runs, unless `review_off` exists, in which case the push goes out as written with detail `review: off`. A `Questions` outcome is treated as `Failed`.
- `pushover: reviewed JSON: {title, action, body[, session, text_id, failed_rules, findings, held, sent_despite_failed_review, held_source, held_identity]}` is printed before any post. `send.py` reads it.
- Failed or Withheld:
  - When the priority is 2 or the push has a registered id that is not agent-written (one with no current approval), it is still sent. Such a registered id sends its rendered text and requests a template fix. The log detail is `sent despite failed review: <rules>`.
  - Otherwise, including `nate.adhoc`, nothing is posted. The command prints `pushover: not sent: <why>` and exits 6. Agent-written ids request no template fix, including at priority 2.
- `Rewritten` sends the rewrite and prints `pushover: sent as rewritten:`, followed by the title, the header line(s) and the body. This includes agent-written ids (`nate.adhoc`, `alert.blocked`, `alert.decision`). Other registered ids always send their rendered text; review is only a logged check, with detail `registered text sent as rendered, rewrite not applied`. Agent-written ids keep their `Template:` footer, reviewed JSON `text_id` and review.jsonl `text_id`.
- `deliver(draft, keys_file, post, *, detail, header)` posts a draft that has already been reviewed. `trimmed_body()` is the only body trim. Keys are read from `~/.config/pushover/env` and are never printed. `post: Post` is injectable, so tests never reach Pushover.

| pushover exit | Meaning |
| --- | --- |
| 0 | Sent (as written, rewritten, or despite a failed review) |
| 1 | Pushover refused the push or could not be reached |
| 2 | Refused before sending, usage error, or keys missing |
| 4 | Another sender's claim on this source's hold is live |
| 5 | Replaced: the claim was cancelled before posting, so nothing was sent |
| 6 | Not sent: failed review or withheld |

### `scripts/message/send.py` outcomes for `--to user`

| Output | Exit | From |
| --- | --- | --- |
| `SENT: to the user` | 0 | pushover 0 |
| `SENT: to the user, rewritten:` + the sent title, header and body lines, not truncated | 0 | pushover 0 with the rewritten block |
| `SENT: to the user, sent despite failed review: [rules]` | 0 | pushover 0 with `sent_despite_failed_review` |
| `HELD: not sent, failed review: <why> This push was not sent because it failed review. Revise it and resend it with send.py; no fallback is allowed.` | 4 | pushover 6 |
| `HELD: <pushover message>` | 4 | pushover 4 (no marker) |
| `HELD: not sent: this push reports an auto-mode refusal. Send it with the auto_mode.refused template: <call>` | 4 | `send()`, before the claim and pushover (no marker) |
| `HELD: not sent: this push reports paid credit use. Send it with the quota.paid_credits template: <call>` | 4 | `send()`, before the claim and pushover (no marker) |
| `HELD: not sent: this push asks the user to type a go. Send it with the session.needs_go template: <call>` | 4 | `send()`, before the claim and pushover (no marker) |
| `REPLACED: a newer version replaced this push; nothing was sent` | 0 | pushover 5 (no marker) |
| `REFUSED: <why>` | 2 | pushover 2 with `refused before sending:` (no marker) |
| `FAILED: the user was not reached: <why>` | 3 | A channel error, missing keys, or a timeout; report the failure. |
| `not sent: probe sessions never push to the phone` | 0 | A session started with `accounts.py exec --probe` |

A failed phone send permits no other phone path.
Legacy failure records remain readable by the existing tools.

- An auto-mode refusal uses `--text-id auto_mode.refused`. A free-text refusal push is held before the claim.
  The check matches `auto mode` or `auto-mode`, plus `refused`, `denied` or `stopped`, in its title or text.
  A resend with the template is not skipped by `--repeat-minutes`.
  The three rca.py pushes in `RCA_TEXTS` are exempt: `permission.rca_started`, `permission.rca_fixed` and `permission.rca_fixed_rewritten`.
  They describe permission stops or refusals. A cause can name an auto-mode refusal.
- Paid credit use goes only through `--text-id quota.paid_credits`. A free-text or agent-written push (`nate.adhoc`) whose title, text or action has `paid credits` or `use_credits` is held the same way (`wrong_template()`). Other registered `quota.*` pushes that name paid credits are not held.
- `--text-id quota.paid_credits`: the caller gives `account`, `machine`, `credits`, `refill` and `next_reset`. `paid_credits_values()` fills `sender` from the session's own record; with no record (a script such as `quota_alert.py` on its timer, or the far side of `--machine`), it uses `--value sender`, then `--from`, and refuses a run with none.
- `--text-id auto_mode.refused`: the session gives `session`, `machine`, one `command` for each refused command (in order), `waits` and an optional `rule`. `command` is the only key that can repeat, and only for this template; `parse()` collects the items in a list. `auto_mode_values()` fills `sender` from the session's own record (`own_session_name()`), `action` from the commands' first words (`rca.action_of()`: `a file change`, `a shell command`, `an action`, `2 shell commands`, or `2 actions` for mixed kinds), `refused` from the commands (one per line), `waits_line` from `waits` (`rca.waits_line()`), `noun` and `this` from the number of commands, and `rule_line` from `rule`. It drops the final periods from `waits` and cuts a command longer than 90 characters to its first 89 and `…`. A send with no `--key` gets the key `auto-mode:<session>:<first 12 hex of the sha256 of the sorted one-line commands>` (`auto_mode_key()`), so `rca.py fixed` can retire the alert. The waits sentence is a value the session writes, so `send()` keeps it only when the cold-read review approves the whole alert within `WAITS_REVIEW_SECONDS` (30 s, `review.review(…, limit=30)`; `reviewed_waits()`). A timeout or a `Failed`, `Rewritten`, `Withheld` or `Unreviewed` verdict sends the alert without it and prints why to stderr. With the review off the alert goes as given. It refuses no command, an empty command, a `waits` that has a thing to type or names a pane or session (`waits` says only what waits; the Action line says what the user does), `--value sender`, `--from`, `--machine` and a run with no session record. `send.py --help` lists the registered pushes (`SESSION_TEMPLATES`): the templates a session sends, and the three rca.py pushes with the rca.py call that sends them. `Options.commands` keeps each `command` value as given, before the cut, for the rca.py case.
  The send rule lives beside `auto_mode.refused` in `send.py --help`. The session applies it before sending; no program checks it.
- After `parse()` renders a registered text, it refuses the run when `action_conflicts()` gives a reason: `<id> gives a second action: <reasons>`. A template value cannot add a second action. Registered texts other than agent-written ids are sent as rendered and skip live review when approved (2026-10-23: a `waits` value told the user to type `! kill …` in a second session).
- `--text-id session.needs_go`: the session gives `session`, `machine`, `go`, `why` and `does`. `needs_go_values()` fills `sender` from the session's own record and drops a final period from `why` and `does`. It refuses the same things as `auto_mode.refused`. A free-text or agent-written push (`nate.adhoc`) whose title, text or action has `type: Go:`, `until you allow`, `until you type` or `waits for your go` is held (`wrong_template()`).
- `user()` passes the message's `--key` to `pushover.py`, in the free-text and the `--text-id` forms, so a kept push with the same key from the same sender replaces the older kept one, and a `--need blocked` push carries the key's tag. `remote()` forwards `--key` to the far `send.py`. `push_texts.user_send_argv(…, key=)` adds `--key`.
- `send.py retire --to user --from SENDER --key KEY` stops the emergency push sent with KEY, once its reason has ended. It prints `retired N queued messages`, then `dropped N kept pushes` (`silence.drop()`: the pushes kept during silence with KEY from SENDER). Then it runs `pushover.py cancel --source SENDER KEY` (`cancel_user_push()`, through `run()` like `user()`, 30 s limit) and prints one line: `cancelled: the emergency push for KEY stopped` (exit 0), `nothing to cancel: no emergency push for KEY is repeating` (exit 0), or `FAILED: the emergency push for KEY may still repeat: <why>` (exit 3). The cancel is by tag with the app token, so it reaches a push sent from either machine that uses the same token. `escalate.py close KEY` calls it for a sent `--need blocked` hold; its pushes carry `--key escalate-KEY`.
- One event sends one emergency push (2026-10-27: two `--key github-cold` sends 23 s apart reached the phone twice). A `--need blocked` send with no `--key` gets `user-emergency:<sender>:<first 12 hex of the title's sha256>` (`user_emergency_key()`). With no `--repeat-minutes`, its window is `pushover.EXPIRE_S` (`emergency_lifetime()`), and `claim(…, live=True)` skips a repeat: `SKIPPED: an emergency push with key K went to the user at T and is still live until E; to send a new one, retire it first: send.py retire --to user --from S --key K`, exit 0. A push that is not `sent` releases its claim (`forget_sent()`), and `retire --to user` forgets the key's last send to the user. An emergency claim saves the owner pid and process start. Only a gone or replaced owner releases an unfinished claim, and a live owner or an unknown result keeps it, so only delivery starts the live window. `remote()` forwards `--repeat-minutes 0`, so only the sending host holds the window. An explicit `--repeat-minutes` wins; note and decision have no window without it.
- An emergency push stops by itself when its cause is fixed (2026-10-27: a `github-cold` push repeated after the user ran `github-warmup` three times). A `--need blocked` (or `--priority 2`) send to the user with neither `--clears-when CMD` nor `--self-clearing` is refused before the claim: `REFUSED: ` and `NO_CLEAR_STEP`, exit 2, nothing pushed, no key claimed. `--self-clearing` (hidden from `--help`) is for a script that retires its own push: `stall_watch.py` (`user_send_argv(…, self_clearing=True)`; `end_prompt()` retires) and `escalate.py deliver()` for a blocked hold (`close` retires). `remote()` forwards `--self-clearing` for every blocked push, and this host keeps the record. A push with `--clears-when` is recorded in `STATE/emergencies.json` under the lock (`record_emergency()`, one record per sender and key). An emergency writes the record at acceptance (`accept_sent`). A `github.keys_cold:` key, or a push that is not an emergency, writes it at the claim, so a warm check also clears an interrupted cold-key claim. The record fields are `key`, `sender`, `check`, `machine` (the host that posted it: this one, or the `--machine` host), `sent` and `expires` (sent + `pushover.EXPIRE_S`). `send.py clear-check` (`clear_check()`) prints nothing when the file is absent. It takes `.clear-check.lock` without waiting (a second run prints `clear-check: another run is in progress`), reads the records, and releases the lock while checks run. Each check runs as `/bin/sh -c CMD` in the home, stdin from `/dev/null`, with `CHECK_TIMEOUT_S` (20 s); past it the process group is killed. One line per record: `retired: KEY from SENDER: its check passed; <cancel line>` (record removed; `retire_user()`, the `retire --to user` path, or `ssh HOST send.py retire …` when `machine` is another host, after `forget_sent()` here), `kept: KEY from SENDER: its check exits N`, `kept: …: its check ran past 20 s and was stopped`, `dropped: KEY from SENDER: its push expired at T` (check not run), or `FAILED: KEY from SENDER: its check passed, but …` (record kept, exit 3). A file with no records is removed. `notifier.sh tick` runs it on both machines after its unlock while the file is non-empty, and logs only a failure to `notifier.log`; a tick with `NOTIFIER_STATE_DIR` set reads only `NOTIFIER_EMERGENCIES`. `github-warmup` runs it once the keys are warm (in /etc/nixos).
- The wait for a user send is raised to at least `review.user_send_seconds()`.
- `--answerable` is an internal flag, valid only with `--to user`. `send.py` sets it automatically when there is no `--from` and the default sender resolved a session name, and `remote()` forwards it. It is recorded in the `Draft` but no longer changes the outcome.
- Remote (`--machine HOST`): `remote()` runs `CLAUDE_PID= send.py --here …` over `ssh -o BatchMode=yes`, so the far side never writes a marker:
  - On failure, the far side prints `send.py: failed push JSON: {…, reviewed, need, held}` to stderr. The calling side reads that record first, so `reviewed` and `need` survive even when ssh drops stdout. It then writes the marker with `held_machine=<host>`.
  - The header is the first line starting with `SENT|HELD|SKIPPED|QUEUED|FAILED|REFUSED|REPLACED: `. Every later line is kept, and the detail is prefixed `on <host>: `.
- Template-fix requests:
  - `send.py template-fix <json>` stamps `template-fixes/<user-zone date>-<sha256(text_id)>` under a per-id lock, so each text id gets at most one notice per day.
  - It then starts a detached `template-fix-deliver`. That sends `Fix push template <id>` with `--from "push template review"` to `TEMPLATE_FIX_SESSION` from `config/notify.conf`, or to `natedev` when that key is unset.
  - Errors go to `template-fix-errors.jsonl`. There is no retry.

`escalate.py close` keeps a sent blocked hold until `send.py retire` succeeds; a failed cancellation still reports its error, and the next close retries it. A review refusal saves `review_held` with a pending `review_reason`. `due` retries forwarding that result until the relay succeeds, then clears the reason; it never resubmits the refused user push.

Silence release discards saved `message.repeat_flood` pushes and records each discard in the push log. Other saved pushes keep their existing delivery rules.

Silence extension stops the old end timer before replacing its saved timer; a failed stop leaves that record for a retry. Each end timer runs `off --timer <unit>`, which checks the saved timer under the silence lock: when a saved silence names another timer it does nothing and exits 0. A stray timer from an interrupted extension cannot end the replacement silence or post its kept pushes. With no saved silence, timer `off` releases leftover pushes, so rerunning the same command retries failed or interrupted delivery. Plain `off` still ends any silence. `silence.claim()` ends silence but leaves the kept-push file intact. `release()` serializes posts under the silence lock and removes each push only after it posts successfully. Failed and interrupted posts stay saved for a repeated `off`; callers skip pushes another release already delivered.

### Legacy holds and claims

Sends no longer create holds: in production code, nothing calls `review.hold()`. The hold machinery stays so that any existing hold is delivered exactly once.

- **Hold file:** `held/<sha256(source)[:16]>.json` (`HeldRecord`). Lookups return `HoldLookup = NoHold | LiveHold(Held) | UnreadableHold`. An unreadable or inconsistent file is moved to `held/bad/<name>.<ns>` and logged.
- **Claim:** a stamped rename to `<id>.sending`, made under `review.lock`. The lock is an in-process `RLock` plus `flock` and can take a deadline. A claim older than `claim_stale_seconds()` is dead and can be taken over.
- **Posting:** `post_claim(held, send) -> ClaimSent | ClaimCancelled | ClaimFailed(code)`. Under the lock, it checks again that the claim still exists, matches `hold_identity` (the sha256 of the sorted hold record) and has no `<id>.cancel` naming that identity. Only then does it post. It writes `held/delivered/<identity>` before releasing the lock. A failed post restores the `.json`.
- **Release:** `release_if_matches(source, identity, *, timeout, clock, sleep) -> ReleaseOutcome`:
  - `delivered`: a delivered record exists.
  - `released`: a matching `.json`, or a stale matching claim, was deleted and a cancel mark was written.
  - `posting`: a matching claim younger than the stale limit exists.
  - `none`: no hold, or a hold with a different identity, which is left alone.
- `retire_claim()` cancels a claimed revision that was rejected and writes `<id>.held`.
- `due()` prunes delivered records older than 3600 s. It then posts holds older than `HOLD_SECONDS` (300 s), and stale claims, one at a time through `post_claim`, logging `expired send failed: …` on failure. Only `hold()` scheduled it, as notifier job `notify-review`, every minute.

### Registered phone admission

Every new phone push requires `--text-id` before review, credentials, claims, or posting.
Use `decision.choices`, `alert.blocked`, or `nate.adhoc` when no specific template fits.

For `decision.choices`, file the question with `/unit:question` or `/showrunner:question`, then pass its record path with `--question`.
Filing a question alone never sends a push.
Decisions use structured choices. Agent-written values receive live review.
Both send.py and pushover.py enforce admission.
A legacy free-text push already kept during silence still releases.
Escalate keeps legacy free-text holds unchanged and requests a template revision from their owner.
New escalate holds retain their registered template and values.


### Script push texts

- `scripts/notify/push_texts.py`:
  - `PushText(id, sender, need, title, action: ActionTemplate | NoActionNeeded, body, examples: tuple[PushExample, ...], purpose, session: SessionTemplate | NoSessionLine, header: bool = True, agent_written: bool = False)` and `PushExample(name, values)`. `purpose` is one line (72 characters at most) that says when the push goes out. It is not part of the fingerprint, so a change to it moves no approval. With `header=False` the phone shows only the title and the body, with no `No action needed.` line. `catalog()` refuses `header=False` on an entry that has an action or a session. `nate.adhoc` uses it, and `fingerprint` includes it only when it is False. `nate.adhoc`, `alert.blocked`, `alert.decision`, `decision.choices`, and `review_watch.acknowledge` have `agent_written=True`; this field is excluded from the fingerprint. `agent_written(text_id: str) -> bool` returns False for unknown ids.
  - `render(id, **values)` and `render_entry(entry, values) -> RenderedPush`. An unknown id, a missing value or an extra value raises `ValueError`.
  - `user_send_argv(rendered, *, sender, machine, priority, timeout)` builds the `send.py --to user --from … --need … --text-id … --value …` argv. User-send wrappers take a rendered push, never a string.
  - `fingerprint(entry)` is the sha256 over the templates and every example's values and filled text.
  - `listing(entries)` gives one line per entry, sorted by id: the id in one column, then its purpose. `shown(entry)` gives the entry's purpose, its template with the `{placeholders}` (Title, Session, Action, body, as the phone lays them out) and its first example filled through `render_entry`. `review.py list` and `review.py show <id>` print them with no network and no reviewer; an unknown id prints `No template named <id>.` and the list, and exits 1. `/nate`, `/nate help` and `/nate usage` print the `adhoc`, `silence` and `show` lines and then `review.py list`; `/nate show <id>` prints `review.py show <id>` (user request, 2026-10-21).
  - `REVIEWED_FILE` and `catalog()`, which loads `push_catalog.ENTRIES` and checks for unique ids and named examples.
- `scripts/notify/push_catalog.py` holds every fixed phone text, from scripts and from nixos, each with realistic examples. A comment says where the examples come from. Entry ids include `shutdown.*`, `quota.*`, `rust.*`, `sweep.disk_floor`, `codex.reset_ready` and `nixos.{disk_low,gpu_stopped,ups_battery,ups_shutdown}`.
- `nixos.gpu_stopped`: sender `gpu`, need `decision` (priority 0 without an in-window `--due`), values `xid`, `meaning`, and `time` as `HH:MM <zone>` in the user's zone; wording lives in `push_catalog.py`. `scripts/notify/gpu_xid.py` owns the code-to-meaning table and CLI: `gpu_xid.py <code>` prints the meaning and exits 0; an unknown code exits 2.
- `nightly.result` reports the branch, finish time, type result and full test result.
  `nightly.not_run` reports the reason that the checks could not start.
  Both texts use sender `nightly`, need `note`, and key `nightly-result`.
  The result requires no action. The failure text includes an action.
  The gate says the next nightly run tries again. The cold credentials example directs the user to run `github-warmup`.
  Silence keeps only the newest push and delivers it when silence ends.
  The gate reads `nightly_result_push=on` and `nightly_result_modules=8` from `merge_gate.conf` at use.
  `off` disables both pushes. The module limit controls how many failed modules the result names.
  A run that never reaches merge_gate.py sends no push unless the timer script sends one.
- `auto_mode.refused` has need `decision`. Auto mode refuses commands; the session continues its other work.
  Title: `{sender}: {session} {noun} refused`. The first body line is `Session: {session}`.
  Action: `On {machine}, in the {session} pane, type: Go: run the refused {noun}`.
  The body starts `Auto mode refused {action} in the {session} session.{waits_line}`.
  Each refused command has its own line. Two paragraphs follow:
  `Go: the session runs {this} {noun} now, and auto mode can refuse {this} {noun} again.`
  `Allow rule: auto mode lets {this} {noun} run from then on.{rule_line}`.
- `session.needs_go` (user approved 2026-10-21, need `decision`): title `{sender}: {session} waits for your go`, `Session: {session}`, Action `On {machine}, in the {session} pane, type: {go}`, then `{why}.` and `Your go {does}.`
- `quota.paid_credits` (user approved 2026-10-21, need `decision`): title `{sender}: {account} now uses paid credits`, Action `To stop it, set use_credits: off in ~/rust/hanadocs/agents/{account}.md on {machine}`, then the credits and refill line, `Leave on (recommended):` and `Turn off:`. `quota_alert.py` sends this id, or `quota.paid_credits_queued` while a workers move is queued. The old paid-credits ids are no longer sent.
- `quota.paid_credits_queued` (agreed with accounts 2026-10-21 under the user's approval): the same title, Action, need and sender rule as `quota.paid_credits`, for when the user queued a workers move. Workers never go to Claude: `Leave on` spends credits until the queued move runs, and `Turn off` stops the workers until it runs. Values: `sender`, `account`, `machine`, `credits`, `refill` (no `next_reset`). `paid_credits_values()` fills its sender, and the paid-credit hold detail names both calls.
- `scripts/notify/push_texts_reviewed.json` has the form `{text_id: {example_name: {sha256, time, reviewer, objections?}}}`:
  - It is read only through `review.text_approvals()`.
  - `texts --approve` writes passing examples only.
  - `--approve --user-spec SOURCE` writes every example whatever the verdict. The `reviewer` field is `user-spec: <source>; reviewer: <model>` and `objections` lists `{rule, quote}`. The command still exits 1 when there are objections.
  - Approving an id keeps records only for its current examples.
  - Hand-written user-spec entries use `push_texts.fingerprint(entry)`.
- `scripts/notify/test_push_texts.py` requires that:
  - every example has a current approval;
  - the title is under 45 characters and the header plus body is under 500 (the Session line counts);
  - no rendered push contains `@`.

  It also fails any literal phone send under `scripts/`, `commands/`, and `codex/` outside `push_texts.py`, `pushover.py`, `send.py`. A literal phone send is `--to user`, `--to=user`, a `pushover.py` path, or `broadcast.send("user", …)`, and the test tracks list mutations and aliases.
- A status note that asks nothing goes to a log or to the requesting session, not the phone. For example, `quota_alert.py` logs `quota status <name>: …`.

### Repeated machine messages

`repeat_guard` counts a resolved recipient once across send.py and detached result routes. A director fallback counts at the director boundary. The first two exact copies pass. Later exact copies to the same receiver are held. Arbitrary prose compares literally, including times, timestamps, PIDs, socket paths, message IDs and placeholders: these may be meaningful. Counter identity version 2 retires the old masked counters without dropping unfinished notices. Exact-text replay retained all 746 historical watchdog holds, so no producer-specific repeat key is needed.

`config/send_repeat_guard.conf` owns holding policy and notice deadlines. The caller has a fixed I/O-free 250 ms safety ceiling; it reads no configuration at import. Policy reads, state I/O, nonblocking locks and publication run inside an isolated checker. Errors, busy locks, corrupt state and timeout admit the message and print a diagnostic. Person and phone exemptions precede worker launch. Lock identity is independent of reloadable capacity; records and previews are bounded.

Recipient counters and aggregate `(sender, literal text)` episodes expire after a full window without a copy.
The sender record holds one owner notice per literal-repeat flood across receivers.
Each receiver-rate flood also has one owner notice.
The guard sends no phone push.
Reading saved state drops old phone notices, regardless of their delivery status.
Completed owner notices stay through the active episode.
Exit 0 with `SENT:` proves delivery; durable `QUEUED:` or `KEPT:` owns its queued copy.
Abnormal termination, empty success and `SKIPPED:` remain unknown and retry until confirmed or the configured age limit.
`notice_max_age_seconds` sets that limit to 86400 seconds after notice creation.
Unfinished entries survive episode expiry and keep an inactive sender record alive.
Completed entries expire with their episode.
The bounded outbox never removes unfinished notices to make room.
At capacity, new attempts admit delivery.
Notices disable expiring repeat claims.
Owner delivery can repeat after a lost acknowledgement.
The rate check counts each sender–receiver pair in its own fixed window.
`receiver_rate_count` sets the threshold in config.
Traffic to different receivers never adds up.
A receiver that reaches the threshold gets one owner notice per active episode.
The notice names that receiver and reports one session.
Rate notices never hold delivery.
Rate counters expire after receiver inactivity.
Each sender keeps at most `max_keys` rate counters.
Decoding removes saved sender-wide rate counters and their active outbox entries without a warning.
Literal counters and owner notices keep their existing rules.

`--from user`, `--human`, phone destinations and the reserved `send-repeat-guard` sender bypass counting. Accounting ownership is chosen before forwarding: the originating send.py records its outcome, including failures; internal SSH calls suppress duplicate rows and return their canonical receiver when available. Standalone transports record themselves. All event writers share one JSONL append helper, with original text, canonical receiver, provenance and observed outcome. An accounting fault never disables delivery.

The notifier detaches one supervised notice job with explicit HOME, XDG, configuration and repeat-state paths. The supervisor owns its process group, including delivery children, and kills descendants at its configured deadline or normal completion on Linux and macOS. Delivery subprocesses inherit this group. There is no nested drain worker or alarm. The nonblocking drain lock prevents overlap. Scheduling progress records admitted attempts, never inspection: insufficient budget leaves the sender cursor and notice order unchanged. An admitted attempt persists its claim and rotation to the tail of the sender’s notice list, then advances the sender cursor before discovery or delivery. Discovery and delivery share one bounded child attempt. A new attempt starts only when the supervisor deadline leaves its full configured budget plus subprocess cleanup; otherwise it stays pending for the next job. No synchronous launcher fallback runs.

With recurring jobs that have enough budget to admit work, accessible state, and notices still within retention, rotation gives later senders and notices their turn despite interrupted attempts. It cannot guarantee acceptance by an unavailable destination or service before expiry under an arbitrary backlog.

`scripts/lib/py scripts/message/repeats.py` reports observed send-log totals here and on the peer through existing SSH selection. `--here --replay` instead simulates decisions without sends or state writes. Independent `safety` labels in replay records check distinct-message holds; historical records without labels have unknown distinctness coverage. Older records without human provenance also have unknown coverage; neither is presented as proven zero. All known phone and person traffic remains protected. Peer reporting requires this command on both machines.

### Permission root cause: `scripts/message/rca.py`

The user asked for this on 2026-10-23: each permission alert starts a root cause analysis, and the user gets one push when it starts and one when it is fixed, so the same command never stops a session again.

- A case is one JSON file, `<id>.json`, in `$XDG_STATE_HOME/message/rca` (else `~/.local/state/message/rca`), with a `.lock` for `flock`. Its fields are `id`, `session`, `machine`, `production` (or `""`), `commands`, `kind` (`prompt` or `auto_mode`), `transcript`, `opened` (UTC ISO time), `status` (`open`, `started` or `fixed`), `owner`, `cause`, `fix`, `commit`, `reminded`, `alerts`, `stopped` (UTC ISO time of the permission stop or refusal; `opened` when the report gives none), `waits` (the waits sentence the alert showed, or `""`), `alert_source` and `alert_key` (the alert's sender and key, `""` when it had no key) and `refusals` (how many times the push review refused the fixed text). `read_case()` gives a case file from before a field existed that field's default (`stopped` reads `opened`). The id is the date in the user's zone and a counter: `261023-1` is the first case of 2026-10-23. Each machine keeps its own cases, so the rca.py commands run on the machine that opened the case.
- `rca.py open --session S --machine M --kind K [--command C …] [--transcript P] [--production <showrunner name>]` makes a case, or, when an open or started case has the same session and the same set of commands (whitespace collapsed, order ignored), adds 1 to its `alerts`. It prints `rca: opened <id>` or `rca: joined <id>`. `open_case(Report)` never raises and the command always exits 0: the alert already went out, so an error only prints `rca: no case opened: …` to stderr.
- The transcript belongs to the selected session. The current staff row supplies its production and unit labels.
- The owner message says `stopped at a permission prompt` for a prompt case.
  For an auto-mode case, it says `had a command refused by auto mode`.
  Both forms say `Find why this happened and fix the source.`
- The owner is the production's showrunner (`showrunners.current_address(name)`). With no production, or when its showrunner does not run, the owner is the `natedev` session. A new case sends the owner one message: `send.py --to <owner> --from rca --key rca-<id> --text …`. The message names the case, session, machine, commands and transcript (no `Transcript:` line when the case has none), and gives four steps: run `~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py start <id> --owner <unit>`; find where the command came from (a work order, a helper prompt, a skill or a script); route the fix to the unit that owns that source, so the command never needs a prompt (an allow rule only when the command is safe and expected); after the fix merges, run `rca.py fixed <id> --cause … --fix … --commit <sha>`, with cause and fix in plain words for a reader who does not know the code (no file names, commands or shas), rewritten once when the push review refuses them. `tell_owner()` waits through the shared `send(argv)` path and appends the UTC time, key and completed outcome to `owner.log`. New cases persist `owner_told=false` before delivery and set it true only after success. The watcher and the auto-mode refusal send defer owner delivery until the next concurrent RCA batch; first notices, due reminders, owner socket lookups and case locks share the remaining 110-second tick budget, below the notifier’s 120-second watchdog. Failed or timed-out notices remain in their cases for the next pass. Direct RCA calls retain the 300-second send limit.
- `rca.py start <id> --owner <unit>` sets `started` and the owner, and sends `permission.rca_started`. `rca.py fixed <id> --cause TEXT --fix TEXT --commit SHA` first runs the cold-read push review (`review.review()`, as pushover.py runs it) on the whole rendered `permission.rca_fixed`, since cause and fix are free text. On the first refusal (`Rewritten`, `Failed` or `Withheld`) it sends nothing, adds 1 to `refusals`, prints `rca: not sent: the push review refused this text:` with the text, the reviewer's failures, questions and rewrite, and exits 1, so the owner writes cause and fix again once. On a second refusal it sends the reviewer's rewrite as `permission.rca_fixed_rewritten`, or the owner's text when the reviewer gave no rewrite. With no reviewer (`Unreviewed`) or the review off, the owner's text goes. When the text goes, `fixed` sets `fixed` and the commit, then retires the case's alert by its sender and key (`send.py retire --to user --from <alert_source> --key <alert_key>`, `push_texts.user_retire_argv`), and then sends the push; a case with no `alert_key` skips the retire. So a kept alert never goes out at silence end, and an emergency alert stops repeating. Each push goes through `push_texts.user_send_argv` and `send.py --to user --text-id … --from <sender> --key rca-<id>`, so while silence is on the fixed push replaces the kept started one. The sender is the production, or `messaging` when there is none. A fixed case takes no `start`; `fixed` can run again to send its push again. A push that send.py does not send exits 1 and keeps the new status; a first refusal exits 1 and keeps the old status; an unknown id exits 2.
- `rca.py list` prints one line per open or started case. `rca.py remind`, and `rca.remind()` at the end of each `stall_watch.py` pass, sends the owner one reminder (`--key rca-<id>-remind`) for each case still `open` 30 minutes after it opened. First notices still pending and due reminders run concurrently, wait for delivery and mark `owner_told` or `reminded` only on exit 0; a failure retries on the next pass. The `start` and `fixed` pushes wait for send.py (`SEND_SECONDS`, 300 s) and print its outcome, because a showrunner runs them by hand and reads it.
- The three texts have need `note` and no action. They name no owner, file, command or commit.
  The commit stays in the case and `owner.log`.
  `story()` fills `time` from `silence.shown(stopped)` and `stop` from the case.
  The stop text is `auto mode refused <kind>` or `the session asked you to allow <kind>`.
  `rca.action_of()` supplies the kind: `a file change`, `a shell command`, `an action`, `2 shell commands` or `2 actions`.
  Only send.py cuts refused commands.
  - `permission.rca_started` title: `{sender}: finding why {session} hit a check`.
    Body: `At {time} in the {session} session, {stop}.{waits_line} A session now finds the cause and fixes it, so this does not happen again. A second push comes when the fix is in.`
    `waits_line` is the case's waits sentence, or `""`.
  - `permission.rca_fixed` title: `{sender}: fixed why {session} hit a check`.
    The body has three lines: `At {time} in the {session} session, {stop}.`, `Why: {cause}.` and `Now: {fix}.`
    The fixed push has no waits sentence. rca.py puts cause and fix on one line each and removes their final periods.
  - `permission.rca_fixed_rewritten`: title `{title}`, body `{body}`: the reviewer's rewrite of a fixed text it refused twice.
- Hooks:
  - `stall_watch.py`: after a 60-second prompt wait, its owner receives the unit, machine and request first. The saved `told` mark stops repeated owner notices. The prompt start plus 60 seconds plus `prompt_grace_seconds` from `config/stall_watch.conf` determines user escalation, even if owner delivery keeps failing. Missing or invalid grace is reported and uses zero grace. A prompt that clears before that deadline sends no push. A prompt alert's `Delivery` carries an `rca.Report` (`prompt_report()`: the unit, the machine, kind `prompt`, the pending Bash call's command or else the pending tool's name, the session's transcript, the showrunner staff name, `stopped` (the wait's start), source `stall-watch` and key `stall-watch:prompt:<showrunner name>:<unit>`). `send_all()` immediately saves a successful alert's `alerted` flag and its report as `case_pending` in the prompt state. After the batch, `retry_cases()` attempts each pending case and clears its report only when the case opened or joined. A failed case retries on later ticks without another alert. When the prompt clears, its alert is retired as usual; successful retirement atomically moves a prompt with a pending case to `<report hash>.case.json` in the same stall-watch state directory. This saved snapshot is solely a case obligation, so a new prompt starts its own wait and can alert while the old case still fails. `retry_cases()` also reads these case files and removes each only after its case opened or joined. With no pending case, retirement deletes the prompt state. Failed alerts still retry with the same alert key.
  - `send.py`: when a local `auto_mode.refused` push ends `sent` or `kept` (silence on), `send()` opens a case with `refusal_report()`: the `session` value (the session with refused commands, not the sender), the `machine` value, kind `auto_mode`, each `--value command=` as given (`Options.commands`, not cut), the waits sentence the alert showed (`""` when the review dropped it), and the alert's sender and key. It defers owner delivery to the next `rca.remind()` pass, so completing the refusal send does not wait for the owner. It gives no transcript, so rca.py finds that session's transcript by the `session` value. A held, replaced, refused or failed push opens no case. `auto_mode.refused` takes no `--machine`, so no remote send opens one.

### Question review in showrunner replies

- **Registration** (`scripts/production/update_registration.py questions --state`):
  - An `open_for_user` entry is either a reminder (a plain one-line string) or an object `{text, after?, question?}`.
  - A new object item (its `text` not already outstanding) must carry `question`. Registration refuses one without it, before any review runs.
  - Duplicate texts are handled once, at their first occurrence.
  - The plumbing check (`dailies_render.plumbing(line) -> str | None`, the `PLUMBING` words) runs here, on each `text`, and nowhere on read (2026-10-18). A new or changed text that fails it is refused (`questions: failed — <text>: …`) and nothing is written. A stored text that fails it is named (`questions: reword — <text>: …`) and the write goes on. `read_outstanding` no longer refuses: one stored word blocked every update to hana's list.
  - Each new question is reviewed once with `review.review_question(QuestionKey(name, text), question, revision=False) -> QuestionOutcome`. This uses guide tags `question,all`, no push header parsing, no length limits and no `withhold`.
  - Outcomes:
    - `QuestionAccepted(question)`: the original, or the rewrite if it passed a second check.
    - `QuestionAwaitingFacts(question, questions)`
    - `QuestionReviewFailed(question, details)`
    - `QuestionReviewUnavailable(question, reason)`: stored as written. A blank rewrite from the reviewer lands here.
  - An awaiting-facts or failed outcome fails the whole registration with `question held for review: …`. Nothing is written: outstanding and history stay unchanged.
  - Such a question is held in `~/.local/state/notify/questions/<sha256(name+text)[:16]>.json` (`QuestionHoldRecord`). Running the same item again counts as its revision. Question holds never reach the post path.
- **Stored fields:** a canonical question record carries `question` and either `reviewed` or `review_unavailable`.
  The [production question store](showrunner-automation.md#updates-time-log-and-questions-update_registrationpy) owns question files and fixed asker ids.
  Existing questions retain their fields without another review.
  Accepted text prints as `reviewed question: <text>`.
  The reply carries that reviewed text.
  `commands/showrunner/produce.md` defines registration before a reply asks the user for input.
- **History:** `record_question_history()` records each new open item in `~/.local/state/showrunner/outstanding/<showrunner staff id>.history.jsonl`.
  Each record contains `text`, `question` and `reviewed`.
  `withdraw` sets `withdrawn` to the withdrawal time on the item's history record.
  History remains for 48 h.
- **No Stop hook checks a reply** (user, 2026-10-19: "i have already asked you to get rid of the stop hook that does this"). The hook file was deleted after `settings.json` stopped naming it. Registering a question and carrying its reviewed text is the showrunner's rule in `commands/showrunner/produce.md`; the nightly count still reads the history.
- **Question detection** (in `review.py`):
  - `question_prose(text)` removes inline code and collapses whitespace.
  - `reply_questions(text) -> ReplyAsks(sentences, has_choices, choice_lines)` skips fenced code and `>` quotes. It finds sentences ending in `?`, and requests that start with reply, choose, pick, confirm, decide, approve, tell me, let me know, say, would you, do you want, shall I or should I. `(recommended` lines are counted as choices.
  - `user_reply_questions(text)` blanks paragraphs addressed to another session before it calls `reply_questions`. `peer_question_lead` recognises:
    - a heading or lead-in that names a recipient with `to` or `for` (unit, unit director, session, worker, agent, showrunner);
    - "sent/relayed/forwarded/message/question to …";
    - "quoted from …" or "… quote/choice/message from …";
    - "… said/wrote/asked/in its own words/own words".

    A user-return marker (`back to you:`, `for you:`, `to you:`, `to the user:`, `question for you:`, `your decision:`) gives the rest of the line back to the user.
- `dailies_render.read_outstanding()` returns `OutstandingReminder` or `OutstandingQuestion`, with `DueNow` or `DeferredUntil` in place of an optional `after`. `open_items()` prints each stored full question.

### Nightly `messages` mode

- `scripts/nightly_review/nightly_review.py`: the modes are `MODES = ("config", "rust", "messages")`. A run on demand does them in that order on natedev; no timer runs them since /etc/nixos cc9a0e9 (2026-10-19). The command scope is `<Scope mode="messages">` in `commands/nightly_review.md`; its one write outside DIR is the `review.jsonl` sample lines.
- `live_message_counts(review.jsonl, since)` counts the live verdicts: messages, failures (`rewrite|ask|failed|withhold`), failures by rule, and `Unreviewed` (`unreviewed:` or `review: off`). It also counts registry templates sent despite a failed review. It skips `replay`, `dry` and `sample` lines and `kind: "question"` lines.
- `showrunner_transcripts(since)` finds transcripts. It maps each `~/.claude/sessions/*.json` record that belongs to a registered showrunner, a production session, or `natedev` to its transcript in that session's own account home. A recorded transcript path is used first. A transcript counts only if it has an assistant reply since the cutoff.
- `messages(since)` runs `review.py sample <transcripts> --limit 20 --since <iso>` with timeout `20 * review.user_send_seconds() + 15`.
- `sample()`:
  - It reads final, non-sidechain `end_turn` replies with `transcript_replies(path, since)`. Replies are filtered by their own timestamp, not file modification time, and each carries `ReplyContext(transcript, date, previous_reply)`.
  - Replies with asks are not reviewed. They count as having an unregistered question when any sentence matches nothing in `registered_questions(since)` (current outstanding plus history lines after the cutoff, fields `text`, `reviewed` and `question`), or when a choices-only reply contains no registered item.
  - It dry-reviews up to `limit` of the newest remaining replies with `reply,all`, logs `sample: true` and posts and holds nothing.
  - It prints `sample: N replies; passed; failed; unreviewed; replies with unregistered questions; unreadable transcripts` and `sample failed rules: …`. It exits 1 only when a transcript was unreadable.
- The nightly pass proposes at most one new guide rule, through `DIR/messages.md` and the ledger, which leads to `/next efficiency`.

### State and config

| Path (`~/.local/state/notify/`; `NOTIFY_STATE_DIR` names another base for review.py, send.py's marker and the gate) | Role |
| --- | --- |
| `pushover.jsonl` (fixed path) | Each post or refusal: time, source, priority, title, posted message, outcome and detail |
| `review.jsonl` | Each review verdict |
| `review.lock` | Hold state lock |
| `review_off` | The user's switch |
| `held/` (`.json`, `.sending`, `.cancel`, `.held`, `bad/`, `delivered/`) | Legacy holds |
| `failed/<session_id>.json`, `failed/.lock` | Fallback markers |
| `questions/<digest>.json` | Held registration questions |
| `template-fixes/`, `template-fix-errors.jsonl` | Daily template-fix stamps and errors |

- `config/notify.conf` holds `REVIEW_TIMEOUT_SECONDS=60` and an optional `TEMPLATE_FIX_SESSION`. `review.NOTIFY_CONFIG` is a module constant, and tests patch it.
- Worker agents are called "workers", not "seats". Worker naming and its migration are in `docs/as-built/long-lived-workers.md` and `docs/accounts.md`, not here.

## Invariants

- **No test or live check reaches the phone.** Tests fake the reviewer `runner` and `post`. Live checks use `review.py check`, `replay`, `texts` or `sample`, and none of these posts. Probe sessions never push.
- **A push is never lost to the reviewer.** A missing binary, timeout, error, quota or unreadable JSON gives `Unreviewed`, and the push goes out as written, logged `unreviewed: <why>`. A failed phone send permits no other phone path.
- **The reviewer never adds a fact.** It may cut, reorder and reword. When facts are missing it asks, and a session push that needs facts is not sent.
- **No skip flag.** Only the user's `review_off` switch turns review off.
- **Form is kept.** An action push stays an action push and a no-action push stays one. The Session line stays above `Action:`. Priority never changes. Pushover limits are a title ≤ 250 and a message ≤ 1024, header and footer included.
- **A push that fails review is not sent** (exit 6, `HELD`). Only registered ids that are not agent-written and emergency (priority 2) pushes still send, logged with the failed rules. A failing registered text requests a template fix only when it is not agent-written. `nate.adhoc` is checked like free text.
- **Use registered phone paths only.** A failed send permits no other phone path.
- **Every script phone push is a catalog entry** sent through `push_texts.user_send_argv` or `--text-id`. A literal phone send under `scripts/` fails `test_push_texts`. Question review is not a push and needs no catalog entry.
- **The approval file is read only through `review.text_approvals()`.** User-spec approvals carry `objections`. Approving an id keeps records for exactly its current examples. The reviewer's rewrites of the user's own text are never applied; agent-written `nate.adhoc` values take the reviewer's rewrite.
- **Every reader of the `Action:` line** (review.py parse, record and check; send.py; pushover.py) keeps a `Session:` line above it, and the length rule counts that line.
- **Question mode never goes through the push first-line parser** and has no phone headers or length limits.
- **Outer waits come from `review.py` helpers** (`user_send_seconds()` and the others), never from literals. Each reviewer call can take up to `review_timeout_seconds()`, and wherever a rewrite is checked again, the budget allows two reviews.
- **A terminal non-delivery** (`SKIPPED`, `REPLACED`) exits 0. Callers treat a nonzero exit as worth a retry or an alarm.
- **Changes to `questions()` keep calling `record_question_history`** with `question` and `reviewed`, so the nightly count matches the reply. Question detection elsewhere reuses `reply_questions`. Recipient context applies only to the Stop hook, through `user_reply_questions`. `reply_questions` and `sample` stay unchanged.
- **Do not commit `settings.json` or `config/models.conf` from a unit worktree;** the showrunner applies the exact lines a change proposes.

## Calibration and gotchas

**Time limits** (R = `REVIEW_TIMEOUT_SECONDS`, default 60; P = `pushover.TIMEOUT_S`, 15), all from `review.delivery_waits()`:

| Helper | Formula | At R=60 |
| --- | --- | --- |
| `revision_seconds()` | 2R + P | 135 s |
| `user_send_seconds()` | revision + 10 | 145 s |
| `claim_stale_seconds()` | max(`CLAIM_STALE_SECONDS` 120, revision + 10) | 145 s |
| `posting_wait_seconds()` | min(110, revision + 5) | 110 s |

- `review_timeout_seconds()` reads the file once per call. A missing file, a missing key, or a non-positive or non-finite value gives 60 and a stderr line.
- The 110 s posting cap keeps the gate inside its 120 s hook timeout. Claude Code's default hook timeout is 60 s.
- Other timing values:
  - `MARKER_MAX_AGE_SECONDS` 600.
  - `HOLD_SECONDS` 300.
  - `DELIVERED_KEEP_SECONDS` 3600.
  - 2 s cap on the model lookup.
  - `send.py` `TIMEOUT` 40; a user send waits at least `user_send_seconds()`.
  - ssh wait: timeout + `KILL_GRACE` 10 + 30.
  - Priority 2 repeats every `RETRY_S` 300 s for up to `EXPIRE_S` 10800 s.
- Live reviews take about 10–13 s each.

**Lengths:**
- The title is under 45 characters and the header plus body is under 500, counted by code. In code the title rule's stem is `push-title-under-45`, which has no rule file; the guide's title rule is `title-names-the-stake`.
- The first 150 characters make sense on their own.
- At most 3 questions.
- The full holdout example sits near the 500 limit with the Session line counted.

**Nightly numbers:**
- Question history is kept 48 h.
- The sample limit is 20, and the default sample window is 24 h.
- The worst case for a sample run is about 20 minutes (20 reviews × 60 s). The subprocess timeout is `20 * user_send_seconds() + 15`.

**Reviewer behaviour:**
- Without explicit instruction the reviewer repeats the action line at the top of the body. `validate` removes it, along with an `Action: ` prefix on the action.
- The reviewer is not deterministic and not always stable on its own rewrite. Approve a catalog text only after three passing runs. Example values decide the verdict as much as templates do: names that are unrealistic or contradict each other fail review.
- Changing a template or an example value invalidates its approval. Rerun `review.py texts --id <id>`, then `--approve`.
- `--user-spec` without `--approve`, or with an empty source, is a usage error. The reviewer objects to the user's four-line holdout text on every run, and those objections are recorded, never applied.

**Holds and the gate:**
- A sender killed mid-post can cause one repeat post after the stale-claim time, because the remote accept and the local release cannot happen together atomically.
- A stale or mismatched marker denies the push and is left in place. A matching marker is consumed before the held and unreviewed checks.
- `--answerable` and the hold files are legacy. Sends no longer create holds, but an existing hold still blocks a second sender (exit 4) and is delivered once by `due`.

**Tests and imports:**
- Test fakes of the reviewer must match the resolved binary by basename (`Path(argv[0]).name == "claude"`), because the resolved path is absolute.
- Dry checks run the worktree's `scripts/notify/review.py`. The installed `~/.claude` copy can lag with an older schema.
- Modules that load in copied trees import `review` lazily, because those trees lack `scripts/notify/`:
  - `sweep.send_floor_alert`
  - `update_registration.review_open_question`
  - `nightly_review.messages()`, with `showrunners` imported only in discovery

**Question checks:**
- A heading that only names a unit or session, with no `to` or `for` and no attribution, is a subject, not a recipient. Questions under it stay the user's. Status replies use such headings often.
- A dry check run against an outstanding snapshot taken after the replies over-reports. Questions registered before their reply and closed before the snapshot read as unregistered.
- The nightly unregistered count includes asks addressed to peers.

## Why

- **Use registered phone paths only.** A failed send permits no other phone path.
- **A headless `claude -p` with no tools, settings or session:** it reads every message cold, the way the user does, and cannot act. It runs on the unit account with the caller's credentials removed, so it never acts as the sending session.
- **Reviewer faults send the push as written:** an outage must never silence an alert to the user.
- **A failed session push is not sent; registry and emergency pushes are:** a live session can revise and resend. A timer or script has no session to revise in time, and holding its push loses it. So the push goes out and a template fix is requested. The hold code stays only so existing holds finish.
- **Code counts lengths:** the model reported false length failures on short bodies.
- **The time limit lives in `config/notify.conf`:** a fixed 20 s constant was too short and could not be configured, and `config/models.conf` is pinned.
- **User-specified texts are approved with recorded objections:** the user's own four lines win over the reviewer's rewrite.
- **No source tracking for example values:** the logs hold no real sends for those senders, so the catalog says where its examples come from instead.
- **No recipient filter in the nightly count:** one day's measurement found 29 of 578 showrunner replies with an unregistered ask. Only one (3.4%) was addressed to a peer only, well under the one-in-four bar, so peer asks are too small a share to matter.
- **No Stop hook for unregistered asks** (user, 2026-10-19: "i have already asked you to get rid of the stop hook that does this"). It once blocked 28 unregistered asks in one day; the nightly count still measures them.
- **One loader for the UX and message guides:** one way to do a thing.


Registered alerts and delivery footer (2026-11-01):
- Every posted push ends in `Sent by: <sender> · Template: <id>` on one line. Legacy kept free text keeps `none (free text)`. The audit reads both this and historical two-line footers.
- `mac.locked`, `github.keys_cold`, `build_hold.blocking` and `nixos.ups_power_back` carry the approved fixed wording. GitHub cold keys need no body. UPS battery and shutdown alerts also use their existing ids.
- `nixos.ups_replace_battery` asks the user to replace the worn UPS battery after REPLBATT.
- `nixos.ups_no_contact` asks the user to check the UPS USB cable after NOCOMM.
- `/alert_user` selects a fixed template first, else `alert.blocked` or `alert.decision`. These two use agent-written title, action and body: short and to the point, with live review like `nate.adhoc`.
- For agent-written pushes and legacy free-text records, the push review receives every fixed template's id and purpose. When one fits, it withholds the draft under `use-registered-template:<id>`, including emergency drafts; it names the required id instead of rewriting around it. Fixed registered live reviews and catalog example reviews do not get this instruction; fixed registered texts still send as rendered and report review failures.
- `send.py` gives `github.keys_cold` a shared `github.keys_cold:<machine>` key in its existing locked claim store. Every sender uses it. Under the claim lock, the warm-check record is saved before the key, so a sender crash before delivery cannot strand a claim without its check; a failed send releases the claim. A delivered event suppresses later sessions until the existing `github-warm-status` clear check retires it. The check survives phone expiry so a continuing cold spell cannot start a second event. Run the template on the machine with cold keys, forwarding delivery with `--machine natedev` on the Mac. No extra ledger or time window.
- New phone pushes require a registered template at both admission boundaries.

A decision uses priority 1 only before an explicit deadline within the configured decision window.
The channel recalculates this priority when it releases a kept push.

A kept decision carries its question path and opening time (`since`).
At release, a closed question drops its push.
An open question with a different `since` also drops its push.
A same-minute reopening keeps `since`, so release sends the push because the question is open again.
A missing or unreadable question record permits delivery and adds an anomaly to `pushover.jsonl`.
A kept `review_watch.acknowledge` push drops when `review_regime.acknowledged()` reports acknowledgment.
Legacy kept pushes without a reference still release.
The release output counts delivered pushes and dropped pushes separately.
Immediate delivery does not apply these release checks.
