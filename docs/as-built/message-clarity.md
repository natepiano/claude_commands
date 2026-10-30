# Message clarity

## What it is

The user wanted every message sent to them to be checked for clarity by an agent that speaks for them. This feature does that. A written message guide sets the rules for pushes, questions and replies. Sessions load the guide with `/message_style` before they write to the user. Before any phone push is posted, a headless reviewer reads it cold against the guide. The reviewer can approve it, rewrite it, ask for missing facts or withhold it. A push that fails review is not sent, and the sender is told to revise it. Claude's built-in `PushNotification` is gated so it cannot bypass the review. Every fixed script push is a catalog entry that has been approved ahead of time. When a showrunner asks the user a question in a reply, the question is registered and reviewed first; no hook checks the reply (user, 2026-10-19). A nightly `messages` pass measures how messages did and proposes new guide rules.

## How it works

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
- `commands/message_style.md` runs the loader on the whole guide and confirms the rule count. `/alert_user`, `/message`, `/message_review`, `/build:report` and the `<Notify>` section of `commands/showrunner/produce.md` load it before their first message to the user in a session.
- Push form:
  - The first line is `Action: <what the user does>` or `No action needed.`, with an optional `Session: <name>` line above it. Every order rule in the guide applies after these lines.
  - The title is under 45 characters, names what is at stake and has no fixed prefix.
  - The first 150 characters make sense on their own.
  - The header plus body is under 500 characters.
- `ask-once-then-count` limits only the end-of-reply reminder, never an answer the user asked for.

### Push data flow

```
session ──send.py --to user──┐
script ──push_texts.user_send_argv → send.py --to user --text-id ID --value k=v──┤
nixos ──pushover.py --text-id ID --value k=v──┤
                                              ▼
pushover.py send(): parse → action checks (refuse = exit 2) → legacy hold lookup (free text and agent-written ids)
  → approved registered text, except agent-written ids: no review | review_off: no review | else review.review(draft)
  → agent-written nate.adhoc is checked like free text: apply rewrites; failed review means exit 6
  → print "pushover: reviewed JSON: {…}"
  → not sent (exit 6) | post via deliver() → post_message() adds Sent by / Template → pushover.jsonl
                                              ▼
send.py user(): map exit code → SENT / HELD / REPLACED / REFUSED / FAILED
  → on FAILED or failed-review HELD under a Claude session: write failed/<session_id>.json
                                              ▼
PushNotification (built-in) ── PreToolUse gate ── allowed only with that marker's exact fallback line
```

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
  - **Model:** `agents_resolve_print message_review.reviewer` sets the model, with a 2 s cap on that lookup. When it resolves nothing, the reviewer uses opus at medium effort. `config/agents.conf` has no `message_review` entry today, so the fallback is what runs.
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
- Footer (user ask, 2026-10-27: an emergency push from the hana session did not say who sent it). `post_message()` is the one place a push goes out, also a push kept during silence (`post_kept()`) and a held push sent at hold expiry. It adds two last lines after the review and the length checks, so a reviewed text and its approval do not change:

  ```
  <message>

  Sent by: hana
  Template: auto_mode.refused
  ```

  - `Sent by` is `sender_name(source)`: the source on one line, at most 80 characters. A `send.py in <session>` source (`send.py`'s `default_sender()`) shows only the session; any other source (`stall-watch`, `rca`, `natedev`, `build-followups`, `send.py`, or the parent command line when there is no `--source`) shows as it is.
  - `Template` is the `--text-id`, or `none (free text)` for a push with no text id.
  - The footer always fits in 1024 characters: a long message is cut at the end first, never the footer.
  - An empty sender is refused: nothing is posted, the log outcome is `refused before sending: no sender`, and the exit is 2.
  - `pushover.jsonl` logs the posted message, footer included. A kept push logs its message without the footer when kept, and with it when posted.
  - Only `pushover.py` holds `api.pushover.net` under `scripts/` (`test_push_texts.py`), so no path posts round the footer.
- An emergency push (priority 2) repeats every 300 s for 3 h until the user taps Acknowledge. Deleting it on the phone does not stop it. With `--key`, the post carries `tags=push_tag(KEY)` (`key-` and the first 16 hex digits of the key's SHA-256: the same key gives the same tag). Below priority 2, or with no key, there is no tag. Priority 2 is never kept during silence, and `post_kept()` passes the kept key, so a tag is never lost.
- `pushover.py cancel [--source NAME] KEY` posts `token` to `receipts/cancel_by_tag/<tag>.json`, which stops every emergency push with the key's tag. It logs to `pushover.jsonl` with title `cancel emergency push` and outcome `cancel: cancelled N`, `cancel: nothing to cancel`, `cancel: refused: …`, `cancel: unreachable: …` or `cancel: keys missing`. It prints `pushover: cancelled N` or `pushover: nothing to cancel` and exits 0; it exits 1 when Pushover refused it or could not be reached, and 2 on usage or keys missing. No receipt is stored. (2026-10-23: a stall-watch prompt push kept repeating after the user answered the prompt.)
- While the user's silence is on (`silence.py`), `deliver()` keeps a push below priority 2 in `silence-kept.jsonl` and silence end posts it. `deliver(…, key=, text_id=)` puts `--key` and `--text-id` in the `KeptPush`, so a released push gets the same footer. A kept push with the same non-empty key from the same source replaces the older kept one (`silence.keep_here()`), so silence end sends only the newest. On the Mac the key goes in the push JSON to natedev's `silence.py keep`. A held push sent after its hold expires has no key (`""`), and an old kept line with no key reads as `""`. An old kept line with no text id reads as `""` and is sent as free text. `silence.py drop --source S --key K` (`silence.drop()`) removes the kept pushes with that source and non-empty key and prints how many; on the Mac it asks natedev over ssh, and gives 0 when natedev cannot answer. `send.py retire --to user` runs it, so each kept push goes out once, and the alert of a closed rca.py case is gone before silence ends. When auto silence ends (`auto_silence.come_back()`), `silence.auto_off` goes first and the kept pushes follow: title `{sender}: you are back`, body `{held}`, one of `No pushes were held while you were away.`, `The push held while you were away comes next.` or `The N pushes held while you were away come next.`
- With `--text-id`, the push is rendered from the catalog. The template sets the sender, the action and the Session line. The priority comes from the template's `need` (`note`→0, `decision`→1, `blocked`→2), unless `--priority 2` is given.
- Refused before sending (exit 2, logged as `refused before sending: <reason>`):
  - no action stated
  - both `--action` and `--no-action`
  - an empty or placeholder action (`none`, `n/a`, `nothing`, `no action`, `-`)
  - `--no-action` at priority > 0
  - an action longer than the space left in 1024 characters

  This logic lives in `user_action.py`: `parse_user_action`, `refused_at`, `FIX` and `REFUSED_BEFORE_SENDING`.
- `action_conflicts(action, body)` gives a reason for each second action in the body (the text after the `Session:` and `Action:` lines) of a push with an action. A second action is a thing to type that the Action line does not hold: `type` or `type:` and its text, or a `! <command>` shell escape (`typed_texts()`). It is also a pane or session name that is not the one the Action line names: `in the X pane`, `X session`, `session named X` (`named_places()`). `a type of`, `type the go`, `the pane` and `a Claude session` name nothing. When the Action line names no pane or session, only the first check applies. Every catalog example gives no reason (`test_user_action.py`).
- `user_action.py` also holds `SessionLine`, `NoSessionLine`, `NotificationSession`, `NO_SESSION_LINE`, `split_session`, `session_prefix` and `first_line(action, session)`. Every reader of the header keeps `Session: …` above `Action: …`.
- `push_texts.render_entry()` drops a trailing `-unit` from a session name in a template value: the `sender` and `session` values, and any value that is a whole unit id such as `types-unit` (`push_texts.shown_value`; reviewer, 2026-10-21). Free text and other values keep every word, `per-unit` and `the trunk-unit plan` included. `parse()` changes no text.
- Review order: the hold lookup runs first for free text and agent-written ids (`nate.adhoc`). Other registered ids skip it and, with a current approval (`push_texts.approval_date()`: every example's `sha256` in `push_texts_reviewed.json` matches `fingerprint()`), skip live review: outcome `Unreviewed`, detail `registered text, approved <date of the newest approval>`. Agent-written ids are checked like free text even with a current approval. Otherwise `review.review()` runs, unless `review_off` exists, in which case the push goes out as written with detail `review: off`. A `Questions` outcome is treated as `Failed`.
- `pushover: reviewed JSON: {title, action, body[, session, text_id, failed_rules, findings, held, sent_despite_failed_review, held_source, held_identity]}` is printed before any post. `send.py` reads it.
- Failed or Withheld:
  - When the priority is 2 or the push has a registered id that is not agent-written (one with no current approval), it is still sent. Such a registered id sends its rendered text and requests a template fix. The log detail is `sent despite failed review: <rules>`.
  - Otherwise, including `nate.adhoc`, nothing is posted. The command prints `pushover: not sent: <why>` and exits 6. Agent-written ids request no template fix, including at priority 2.
- `Rewritten` sends the rewrite and prints `pushover: sent as rewritten:`, followed by the title, the header line(s) and the body. This includes agent-written ids (`nate.adhoc`). Other registered ids always send their rendered text; review is only a logged check, with detail `registered text sent as rendered, rewrite not applied`. Agent-written ids keep their `Template:` footer, reviewed JSON `text_id` and review.jsonl `text_id`.
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
| `FAILED: the user was not reached: <why> fallback text:` + one line, or `… <UNREVIEWED_FALLBACK>` | 3 | Any other failure: exit 1, keys missing, timeout |
| `not sent: probe sessions never push to the phone` | 0 | A session started with `accounts.py exec --probe` |

- `FailedPush(title, action, body, hold, reviewed, need, held)`:
  - `fallback_allowed` is true when `(reviewed and not held) or need == "blocked"`.
  - `text` is the fallback line: `<title> — <header>` with whitespace collapsed (the `Session:` line included when present), at most 200 characters, cut at a word boundary with `…`.
  - `reviewed` is true only when the text came from a `pushover: reviewed JSON:` line.
- `user_failure()` writes the fallback marker `~/.local/state/notify/failed/<session_id>.json` under `failed/.lock`. It does this only when `$CLAUDE_PID` resolves a `sessionId` from `~/.claude/sessions/<pid>.json`, and never on a refusal. The marker holds `time`, `title`, `action`, `body`, `message`, `fallback`, `reviewed`, `need`, `held`, and optionally `held_source`, `held_identity` and `held_machine` (empty means local).
- An auto-mode refusal goes only through `--text-id auto_mode.refused`. A push without it whose title or text has `auto mode` (or `auto-mode`) and `refused`, `denied` or `stopped` is held, before the claim, so a resend with the template is not skipped by `--repeat-minutes`. The three rca.py pushes (`RCA_TEXTS`: `permission.rca_started`, `permission.rca_fixed` and `permission.rca_fixed_rewritten`) are not held: their titles say the session stopped, and a cause can say that auto mode refused a command.
- Paid credit use goes only through `--text-id quota.paid_credits`. A free-text or agent-written push (`nate.adhoc`) whose title, text or action has `paid credits` or `use_credits` is held the same way (`wrong_template()`). Other registered `quota.*` pushes that name paid credits are not held.
- `--text-id quota.paid_credits`: the caller gives `account`, `machine`, `credits`, `refill` and `next_reset`. `paid_credits_values()` fills `sender` from the session's own record; with no record (a script such as `quota_alert.py` on its timer, or the far side of `--machine`), it uses `--value sender`, then `--from`, and refuses a run with none.
- `--text-id auto_mode.refused`: the session gives `session`, `machine`, one `command` for each refused command (in order), `waits` and an optional `rule`. `command` is the only key that can repeat, and only for this template; `parse()` collects the items in a list. `auto_mode_values()` fills `sender` from the session's own record (`own_session_name()`), `action` from the commands' first words (`rca.action_of()`: `a file change`, `a shell command`, `an action`, `2 shell commands`, or `2 actions` for mixed kinds), `refused` from the commands (one per line), `waits_line` from `waits` (`rca.waits_line()`), `noun` and `this` from the number of commands, and `rule_line` from `rule`. It drops the final periods from `waits` and cuts a command longer than 90 characters to its first 89 and `…`. A send with no `--key` gets the key `auto-mode:<session>:<first 12 hex of the sha256 of the sorted one-line commands>` (`auto_mode_key()`), so `rca.py fixed` can retire the alert. The waits sentence is a value the session writes, so `send()` keeps it only when the cold-read review approves the whole alert within `WAITS_REVIEW_SECONDS` (30 s, `review.review(…, limit=30)`; `reviewed_waits()`). A timeout or a `Failed`, `Rewritten`, `Withheld` or `Unreviewed` verdict sends the alert without it and prints why to stderr. With the review off the alert goes as given. It refuses no command, an empty command, a `waits` that has a thing to type or names a pane or session (`waits` says only what waits; the Action line says what the user does), `--value sender`, `--from`, `--machine` and a run with no session record. `send.py --help` lists the registered pushes (`SESSION_TEMPLATES`): the templates a session sends, and the three rca.py pushes with the rca.py call that sends them. `Options.commands` keeps each `command` value as given, before the cut, for the rca.py case.
- After `parse()` renders a registered text, it refuses the run when `action_conflicts()` gives a reason: `<id> gives a second action: <reasons>`. A template value cannot add a second action. Registered texts other than agent-written ids are sent as rendered and skip live review when approved (2026-10-23: a `waits` value told the user to type `! kill …` in a second session).
- `--text-id session.needs_go`: the session gives `session`, `machine`, `go`, `why` and `does`. `needs_go_values()` fills `sender` from the session's own record and drops a final period from `why` and `does`. It refuses the same things as `auto_mode.refused`. A free-text or agent-written push (`nate.adhoc`) whose title, text or action has `type: Go:`, `until you allow`, `until you type` or `waits for your go` is held (`wrong_template()`).
- `user()` passes the message's `--key` to `pushover.py`, in the free-text and the `--text-id` forms, so a kept push with the same key from the same sender replaces the older kept one, and a `--need blocked` push carries the key's tag. `remote()` forwards `--key` to the far `send.py`. `push_texts.user_send_argv(…, key=)` adds `--key`.
- `send.py retire --to user --from SENDER --key KEY` stops the emergency push sent with KEY, once its reason has ended. It prints `retired N queued messages`, then `dropped N kept pushes` (`silence.drop()`: the pushes kept during silence with KEY from SENDER). Then it runs `pushover.py cancel --source SENDER KEY` (`cancel_user_push()`, through `run()` like `user()`, 30 s limit) and prints one line: `cancelled: the emergency push for KEY stopped` (exit 0), `nothing to cancel: no emergency push for KEY is repeating` (exit 0), or `FAILED: the emergency push for KEY may still repeat: <why>` (exit 3). The cancel is by tag with the app token, so it reaches a push sent from either machine that uses the same token. `escalate.py close KEY` calls it for a sent `--need blocked` hold; its pushes carry `--key escalate-KEY`.
- One event sends one emergency push (2026-10-27: two `--key github-cold` sends 23 s apart reached the phone twice). A `--need blocked` send with no `--key` gets `user-emergency:<sender>:<first 12 hex of the title's sha256>` (`user_emergency_key()`). With no `--repeat-minutes`, its window is `pushover.EXPIRE_S` (`emergency_lifetime()`), and `claim(…, live=True)` skips a repeat: `SKIPPED: an emergency push with key K went to the user at T and is still live until E; to send a new one, retire it first: send.py retire --to user --from S --key K`, exit 0. A push that is not `sent` releases its claim (`forget_sent()`), and `retire --to user` forgets the key's last send to the user. `remote()` forwards `--repeat-minutes 0`, so only the sending host holds the window. An explicit `--repeat-minutes` wins; note and decision have no window without it.
- An emergency push stops by itself when its cause is fixed (2026-10-27: a `github-cold` push repeated after the user ran `github-warmup` three times). A `--need blocked` (or `--priority 2`) send to the user with neither `--clears-when CMD` nor `--self-clearing` is refused before the claim: `REFUSED: ` and `NO_CLEAR_STEP`, exit 2, nothing pushed, no key claimed. `--self-clearing` (hidden from `--help`) is for a script that retires its own push: `stall_watch.py` (`user_send_argv(…, self_clearing=True)`; `end_prompt()` retires) and `escalate.py deliver()` for a blocked hold (`close` retires). `remote()` forwards `--self-clearing` for every blocked push, and this host keeps the record. A sent push with `--clears-when` is recorded in `STATE/emergencies.json` under the lock (`record_emergency()`, one record per sender and key): `key`, `sender`, `check`, `machine` (the host that posted it: this one, or the `--machine` host), `sent` and `expires` (sent + `pushover.EXPIRE_S`). `send.py clear-check` (`clear_check()`) prints nothing when the file is absent. It takes `.clear-check.lock` without waiting (a second run prints `clear-check: another run is in progress`), reads the records, and releases the lock while checks run. Each check runs as `/bin/sh -c CMD` in the home, stdin from `/dev/null`, with `CHECK_TIMEOUT_S` (20 s); past it the process group is killed. One line per record: `retired: KEY from SENDER: its check passed; <cancel line>` (record removed; `retire_user()`, the `retire --to user` path, or `ssh HOST send.py retire …` when `machine` is another host, after `forget_sent()` here), `kept: KEY from SENDER: its check exits N`, `kept: …: its check ran past 20 s and was stopped`, `dropped: KEY from SENDER: its push expired at T` (check not run), or `FAILED: KEY from SENDER: its check passed, but …` (record kept, exit 3). A file with no records is removed. `notifier.sh tick` runs it on both machines after its unlock while the file is non-empty, and logs only a failure to `notifier.log`; a tick with `NOTIFIER_STATE_DIR` set reads only `NOTIFIER_EMERGENCIES`. `github-warmup` runs it once the keys are warm (in /etc/nixos).
- The wait for a user send is raised to at least `review.user_send_seconds()`.
- `--answerable` is an internal flag, valid only with `--to user`. `send.py` sets it automatically when there is no `--from` and the default sender resolved a session name, and `remote()` forwards it. It is recorded in the `Draft` but no longer changes the outcome.
- Remote (`--machine HOST`): `remote()` runs `CLAUDE_PID= send.py --here …` over `ssh -o BatchMode=yes -o ConnectTimeout=10`, so the far side never writes a marker:
  - On failure, the far side prints `send.py: failed push JSON: {…, reviewed, need, held}` to stderr. The calling side reads that record first, so `reviewed` and `need` survive even when ssh drops stdout. It then writes the marker with `held_machine=<host>`.
  - The header is the first line starting with `SENT|HELD|SKIPPED|QUEUED|FAILED|REFUSED|REPLACED: `. Every later line is kept, and the detail is prefixed `on <host>: `.
- Template-fix requests:
  - `send.py template-fix <json>` stamps `template-fixes/<user-zone date>-<sha256(text_id)>` under a per-id lock, so each text id gets at most one notice per day.
  - It then starts a detached `template-fix-deliver`. That sends `Fix push template <id>` with `--from "push template review"` to `TEMPLATE_FIX_SESSION` from `config/notify.conf`, or to `natedev` when that key is unset.
  - Errors go to `template-fix-errors.jsonl`. There is no retry.

`escalate.py close` keeps a sent blocked hold until `send.py retire` succeeds; a failed cancellation still reports its error, and the next close retries it. A review refusal saves `review_held` with a pending `review_reason`. `due` retries forwarding that result until the relay succeeds, then clears the reason; it never resubmits the refused user push.

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

### The PushNotification gate: `scripts/hooks/pre-tool-use-push-gate.py`

The gate is a PreToolUse hook. Its `settings.json` entry has matcher `PushNotification` and `"timeout": 120`. Built-in PushNotification takes only `message`: one line, no title.

1. With no marker for the `session_id`, an age outside 0–600 s, or a `message` that does not match the marker's `fallback` (whitespace-normalised), the gate denies with `DENIAL`, which points to `send.py --to user`. The marker is left in place.
2. On a match, valid authorization stays in place until delivery or its existing expiry: a PreToolUse allow does not prove the push ran. A stopped or failed built-in push can retry the same reviewed fallback within the original ten-minute window; allowing it does not renew that window.
3. If `held` is true and `need` is not `blocked`, it deletes the marker under `failed/.lock` and denies with `HELD_FALLBACK`.
4. If `reviewed` is false and `need` is not `blocked`, it deletes the marker under `failed/.lock` and denies with `UNREVIEWED_FALLBACK`. A marker without `reviewed` counts as reviewed.
5. If `held_source` and `held_identity` are present, it releases the hold, locally or over ssh with `review.py release`. While the answer is `posting`, it asks again each second until `posting_wait_seconds()` has passed. `delivered` deletes the unchanged marker under `failed/.lock` and denies with `ALREADY_SENT`; a replacement marker is preserved. Every other answer, and a timeout, allows the push and retains authorization for retry.
6. Otherwise it allows the push. Any exception, including failing to import `review`, also allows the push and prints `push gate: <type>: <error>` to stderr.

`post-tool-use-push-delivered.py` runs through the PostToolUse dispatcher only for `PushNotification`. After a successful push (`tool_response.success` is true and `is_error` is not true), it consumes the matching fallback marker under the same `failed/.lock`. The gate and delivery hook share the marker path and whitespace rule. A delivered fallback therefore uses its approval once; stopping before delivery still permits a retry. A missing or unreadable marker, a failed push, or a different message leaves approval unchanged. Any hook error prints a short stderr line and exits successfully. No settings change is needed.

### Script push texts

- `scripts/notify/push_texts.py`:
  - `PushText(id, sender, need, title, action: ActionTemplate | NoActionNeeded, body, examples: tuple[PushExample, ...], purpose, session: SessionTemplate | NoSessionLine, header: bool = True, agent_written: bool = False)` and `PushExample(name, values)`. `purpose` is one line (72 characters at most) that says when the push goes out. It is not part of the fingerprint, so a change to it moves no approval. With `header=False` the phone shows only the title and the body, with no `No action needed.` line. `catalog()` refuses `header=False` on an entry that has an action or a session. `nate.adhoc` uses it, and `fingerprint` includes it only when it is False. Only `nate.adhoc` has `agent_written=True`; this field is excluded from the fingerprint. `agent_written(text_id: str) -> bool` returns False for unknown ids.
  - `render(id, **values)` and `render_entry(entry, values) -> RenderedPush`. An unknown id, a missing value or an extra value raises `ValueError`.
  - `user_send_argv(rendered, *, sender, machine, priority, timeout)` builds the `send.py --to user --from … --need … --text-id … --value …` argv. User-send wrappers take a rendered push, never a string.
  - `fingerprint(entry)` is the sha256 over the templates and every example's values and filled text.
  - `listing(entries)` gives one line per entry, sorted by id: the id in one column, then its purpose. `shown(entry)` gives the entry's purpose, its template with the `{placeholders}` (Title, Session, Action, body, as the phone lays them out) and its first example filled through `render_entry`. `review.py list` and `review.py show <id>` print them with no network and no reviewer; an unknown id prints `No template named <id>.` and the list, and exits 1. `/nate`, `/nate help` and `/nate usage` print the `adhoc`, `silence` and `show` lines and then `review.py list`; `/nate show <id>` prints `review.py show <id>` (user request, 2026-10-21).
  - `REVIEWED_FILE` and `catalog()`, which loads `push_catalog.ENTRIES` and checks for unique ids and named examples.
- `scripts/notify/push_catalog.py` holds every fixed phone text, from scripts and from nixos, each with realistic examples. A comment says where the examples come from. Entry ids include `shutdown.*`, `quota.*`, `rust.*`, `sweep.disk_floor`, `codex.reset_ready` and `nixos.{disk_low,ups_battery,ups_shutdown}`.
- `auto_mode.refused` (user approved 2026-10-21, need `decision`): title `{sender}: auto mode stopped {session}`, `Session: {session}`, Action `On {machine}, in the {session} pane, approve the prompt or add an allow rule`, then `Auto mode refused {action} in the {session} session.{waits_line}`, each refused command on its own line, `Approve: the work goes on now, and auto mode can stop it on {this} {noun} again.` and `Allow rule: the work goes on now, and auto mode lets {this} {noun} run from then on.{rule_line}`.
- `session.needs_go` (user approved 2026-10-21, need `decision`): title `{sender}: {session} waits for your go`, `Session: {session}`, Action `On {machine}, in the {session} pane, type: {go}`, then `{why}.` and `Your go {does}.`
- `quota.paid_credits` (user approved 2026-10-21, need `decision`): title `{sender}: {account} now uses paid credits`, Action `To stop it, set use_credits: off in ~/rust/hanadocs/agents/{account}.md on {machine}`, then the credits and refill line, `Leave on (recommended):` and `Turn off:`. `quota_alert.py` sends this id, or `quota.paid_credits_queued` while a workers move is queued. The old paid-credits ids are no longer sent.
- `quota.paid_credits_queued` (agreed with accounts-unit 2026-10-21 under the user's approval): the same title, Action, need and sender rule as `quota.paid_credits`, for when the user queued a workers move. Workers never go to Claude: `Leave on` spends credits until the queued move runs, and `Turn off` stops the workers until it runs. Values: `sender`, `account`, `machine`, `credits`, `refill` (no `next_reset`). `paid_credits_values()` fills its sender, and the paid-credit hold detail names both calls.
- Shutdown holdout texts. `settle.HoldoutKind.from_reason` picks one of two entries:
  - `shutdown.holdout` when a form is shown.
  - `shutdown.holdout_unanswered` for any other hold.

  `shutdown.holdout` uses the user's four lines: title `{session} {operation} is blocked`, `Session: {session}`, the Action line and `Reason: …`. Two choice lines follow, each saying what it does: `Answer the form (recommended): the session pauses, and the {operation} goes on.` and `Stop the session: it ends, its uncommitted work stays, and the {operation} goes on without it. In a terminal on {machine}, run:`, then the command on its own line. The user found the old `Or stop it instead` line unclear (change review C9, 2026-10-19). `shutdown.holdout_unanswered` keeps the Session and Action lines, but its title is `Your Claude {operation} is stuck`. Its body says the session has not paused and that stopping it keeps its uncommitted work, then gives the bare command. The user's title and `Reason:` line failed the push review here (reader-starts-cold, title-names-the-stake, short-plain-sentences) and were sent anyway as a registry push, so the showrunner had the text rewritten on 2026-10-16. Both give a stop command, which names the account label (`note_file.canonical_label`) with `--here` and never the login. No raw hold-detail list reaches the push; the requesting session's message keeps it.
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

  It also fails any literal phone send under `scripts/` outside `push_texts.py`, `pushover.py`, `send.py` and `escalate.py`. A literal phone send is `--to user`, `--to=user`, a `pushover.py` path, or `broadcast.send("user", …)`, and the test tracks list mutations and aliases.
- A status note that asks nothing goes to a log or to the requesting session, not the phone. For example, `quota_alert.py` logs `quota status <name>: …`.

### Permission root cause: `scripts/message/rca.py`

The user asked for this on 2026-10-23: each permission alert starts a root cause analysis, and the user gets one push when it starts and one when it is fixed, so the same command never stops a session again.

- A case is one JSON file, `<id>.json`, in `$XDG_STATE_HOME/message/rca` (else `~/.local/state/message/rca`), with a `.lock` for `flock`. Its fields are `id`, `session`, `machine`, `production` (or `""`), `commands`, `kind` (`prompt` or `auto_mode`), `transcript`, `opened` (UTC ISO time), `status` (`open`, `started` or `fixed`), `owner`, `cause`, `fix`, `commit`, `reminded`, `alerts`, `stopped` (UTC ISO time the session stopped; `opened` when the report gives none), `waits` (the waits sentence the alert showed, or `""`), `alert_source` and `alert_key` (the alert's sender and key, `""` when it had no key) and `refusals` (how many times the push review refused the fixed text). `read_case()` gives a case file from before a field existed that field's default (`stopped` reads `opened`). The id is the date in the user's zone and a counter: `261023-1` is the first case of 2026-10-23. Each machine keeps its own cases, so the rca.py commands run on the machine that opened the case.
- `rca.py open --session S --machine M --kind K [--command C …] [--transcript P] [--production SLUG]` makes a case, or, when an open or started case has the same session and the same set of commands (whitespace collapsed, order ignored), adds 1 to its `alerts`. It prints `rca: opened <id>` or `rca: joined <id>`. `open_case(Report)` never raises and the command always exits 0: the alert already went out, so an error only prints `rca: no case opened: …` to stderr.
- The transcript is always the stopped session's. With no `--transcript`, `transcript_of()` finds the live session that `--session` names (`sessions.named()` on `sessions.live_sessions()`: `addressed()` with the name as given, then without and with the `-unit` suffix, so `handler-unit` and `handler` find the same session), and takes its newest `~/.claude/projects/*/<sessionId>.jsonl` (`sessions.latest_transcript()`, which `stall_watch.py` also uses). When no live session on this machine has the name, the case has no transcript. The caller's own session (`CLAUDE_CODE_SESSION_ID`) is never used: a showrunner that opens a case by hand, or a session that sends `auto_mode.refused` for another session, is not the stopped session. With no `--production`, the production is the `SHOWRUNNER_UNIT` mark (`unit_lookup.PRODUCTION_MARK`) in the environment, which every process of a unit's session inherits. `showrunners.canonical_slug` maps a former name to the current slug.
- The owner is the production's showrunner (`showrunners.current_address(slug)`). With no production, or when its showrunner does not run, the owner is the `natedev` session. A new case sends the owner one message: `send.py --to <owner> --from rca --key rca-<id> --text …`. The message names the case, session, machine, commands and transcript (no `Transcript:` line when the case has none), and gives four steps: run `~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py start <id> --owner <unit>`; find where the command came from (a work order, a helper prompt, a skill or a script); route the fix to the unit that owns that source, so the command never needs a prompt (an allow rule only when the command is safe and expected); after the fix merges, run `rca.py fixed <id> --cause … --fix … --commit <sha>`, with cause and fix in plain words for a reader who does not know the code (no file names, commands or shas), rewritten once when the push review refuses them. `tell_owner()` starts that send.py detached (`start_new_session=True`, stdin from `/dev/null`) and does not wait for it, so the session or `stall_watch.py` pass that opened the case does not wait for a relay (up to 300 s). It appends a `<UTC time> <key>` line to `owner.log` in the case directory, and send.py's output follows. A message that does not start prints `rca: <id>: the owner message did not start, a reminder follows: …` to stderr. A message that fails after it starts is seen only in `owner.log`. In both cases the case stays open, and the reminder sends the message again.
- `rca.py start <id> --owner <unit>` sets `started` and the owner, and sends `permission.rca_started`. `rca.py fixed <id> --cause TEXT --fix TEXT --commit SHA` first runs the cold-read push review (`review.review()`, as pushover.py runs it) on the whole rendered `permission.rca_fixed`, since cause and fix are free text. On the first refusal (`Rewritten`, `Failed` or `Withheld`) it sends nothing, adds 1 to `refusals`, prints `rca: not sent: the push review refused this text:` with the text, the reviewer's failures, questions and rewrite, and exits 1, so the owner writes cause and fix again once. On a second refusal it sends the reviewer's rewrite as `permission.rca_fixed_rewritten`, or the owner's text when the reviewer gave no rewrite. With no reviewer (`Unreviewed`) or the review off, the owner's text goes. When the text goes, `fixed` sets `fixed` and the commit, then retires the case's alert by its sender and key (`send.py retire --to user --from <alert_source> --key <alert_key>`, `push_texts.user_retire_argv`), and then sends the push; a case with no `alert_key` skips the retire. So a kept alert never goes out at silence end, and an emergency alert stops repeating. Each push goes through `push_texts.user_send_argv` and `send.py --to user --text-id … --from <sender> --key rca-<id>`, so while silence is on the fixed push replaces the kept started one. The sender is the production, or `messaging` when there is none. A fixed case takes no `start`; `fixed` can run again to send its push again. A push that send.py does not send exits 1 and keeps the new status; a first refusal exits 1 and keeps the old status; an unknown id exits 2.
- `rca.py list` prints one line per open or started case. `rca.py remind`, and `rca.remind()` at the end of each `stall_watch.py` pass, sends the owner one reminder (`--key rca-<id>-remind`) for each case still `open` 30 minutes after it opened. The reminder also starts detached, and `reminded` is set when it starts; a reminder that does not start is tried on the next pass. The `start` and `fixed` pushes wait for send.py (`SEND_SECONDS`, 300 s) and print its outcome, because a showrunner runs them by hand and reads it.
- The three texts (need `note`, no action, user ask 2026-10-23; approved under a user spec, 2026-10-23 19:20 AST via the showrunner). They use plain words and name no owner, file, command or commit; the commit stays in the case and `owner.log`. `story()` fills `time` (`silence.shown()` of `stopped`) and `stop`: `auto mode refused <kind>` or `it asked you to allow <kind>`, with the kind from `rca.action_of()` (`a file change`, `a shell command`, `an action`, `2 shell commands`, `2 actions`). rca.py no longer cuts commands; only send.py cuts a refused command.
  - `permission.rca_started`: title `{sender}: finding why {session} stopped`, body `The {session} session stopped at {time} when {stop}.{waits_line} A session now finds the cause and fixes it, so it does not stop there again. A second push comes when the fix is in.` `waits_line` is the case's `waits` sentence, or `""`.
  - `permission.rca_fixed`: title `{sender}: fixed why {session} stopped`, body `The {session} session stopped at {time} when {stop}.`, `Why: {cause}.` and `Now: {fix}.` It has no waits sentence, since nothing waits once the stop is fixed. rca.py collapses cause and fix to one line and drops a final period so the template's own period never doubles.
  - `permission.rca_fixed_rewritten`: title `{title}`, body `{body}`: the reviewer's rewrite of a fixed text it refused twice.
- Hooks:
  - `stall_watch.py`: a prompt alert's `Delivery` carries an `rca.Report` (`prompt_report()`: the unit, the machine, kind `prompt`, the pending Bash call's command or else the pending tool's name, the session's transcript, the production slug, `stopped` (the wait's start), source `stall-watch` and key `stall-watch:prompt:<slug>:<unit>`). `send_all()` immediately saves a successful alert's `alerted` flag and its report as `case_pending` in the prompt state. After the batch, `retry_cases()` attempts each pending case and clears its report only when the case opened or joined. A failed case retries on later ticks without another alert. When the prompt clears, its alert is retired as usual; successful retirement atomically moves a prompt with a pending case to `<report hash>.case.json` in the same stall-watch state directory. This saved snapshot is solely a case obligation, so a new prompt starts its own wait and can alert while the old case still fails. `retry_cases()` also reads these case files and removes each only after its case opened or joined. With no pending case, retirement deletes the prompt state. Failed alerts still retry with the same alert key.
  - `send.py`: when a local `auto_mode.refused` push ends `sent` or `kept` (silence on), `send()` opens a case with `refusal_report()`: the `session` value (the stopped session, not the sender), the `machine` value, kind `auto_mode`, each `--value command=` as given (`Options.commands`, not cut), the waits sentence the alert showed (`""` when the review dropped it), and the alert's sender and key. It gives no transcript, so rca.py finds the stopped session's by the `session` value. A held, replaced, refused or failed push opens no case. `auto_mode.refused` takes no `--machine`, so no remote send opens one.

### Question review in showrunner replies

- **Registration** (`scripts/production/update_registration.py questions --state`):
  - An `open_for_user` entry is either a reminder (a plain one-line string) or an object `{text, after?, question?}`.
  - A new object item (its `text` not already outstanding) must carry `question`. Registration refuses one without it, before any review runs.
  - Duplicate texts are handled once, at their first occurrence.
  - The plumbing check (`dailies_render.plumbing(line) -> str | None`, the `PLUMBING` words) runs here, on each `text`, and nowhere on read (2026-10-18). A new or changed text that fails it is refused (`questions: failed — <text>: …`) and nothing is written. A stored text that fails it is named (`questions: reword — <text>: …`) and the write goes on. `read_outstanding` no longer refuses: one stored word blocked every update to hana's list.
  - Each new question is reviewed once with `review.review_question(QuestionKey(slug, text), question, revision=False) -> QuestionOutcome`. This uses guide tags `question,all`, no push header parsing, no length limits and no `withhold`.
  - Outcomes:
    - `QuestionAccepted(question)`: the original, or the rewrite if it passed a second check.
    - `QuestionAwaitingFacts(question, questions)`
    - `QuestionReviewFailed(question, details)`
    - `QuestionReviewUnavailable(question, reason)`: stored as written. A blank rewrite from the reviewer lands here.
  - An awaiting-facts or failed outcome fails the whole registration with `question held for review: …`. Nothing is written: outstanding and history stay unchanged.
  - Such a question is held in `~/.local/state/notify/questions/<sha256(slug+text)[:16]>.json` (`QuestionHoldRecord`). Running the same item again counts as its revision. Question holds never reach the post path.
- **Stored fields:** an outstanding item carries `question` plus either `reviewed` or `review_unavailable`. Items that were already outstanding keep their fields and are not reviewed again. Accepted text is printed as `reviewed question: <text>`, and the reply must carry that text. `commands/showrunner/produce.md` (under `open_for_user`) tells the showrunner to register before any reply asks the user a question, makes a request such as "Say go and I'll …", or offers `(recommended` choices.
- **History:** `record_question_history()` appends each newly outstanding item's `text`, `question` and `reviewed` to `~/.local/state/showrunner/outstanding/<slug>.history.jsonl`. History is kept for 48 h.
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

- `scripts/nightly_review/nightly_review.py`: the modes are `MODES = ("config", "rust", "messages")`. The timer runs them in that order at 01:00 on natedev. The command scope is `<Scope mode="messages">` in `commands/nightly_review.md`; its one write outside DIR is the `review.jsonl` sample lines.
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
- **A push is never lost to the reviewer.** A missing binary, timeout, error, quota or unreadable JSON gives `Unreviewed`, and the push goes out as written, logged `unreviewed: <why>`. Any gate error allows the fallback.
- **The reviewer never adds a fact.** It may cut, reorder and reword. When facts are missing it asks, and a session push that needs facts is not sent.
- **No skip flag.** Only the user's `review_off` switch turns review off.
- **Form is kept.** An action push stays an action push and a no-action push stays one. The Session line stays above `Action:`. Priority never changes. Pushover limits are a title ≤ 250 and a message ≤ 1024, header and footer included.
- **A push that fails review is not sent** (exit 6, `HELD`). Only registered ids that are not agent-written and emergency (priority 2) pushes still send, logged with the failed rules. A failing registered text requests a template fix only when it is not agent-written. `nate.adhoc` is checked like free text.
- **The fallback is reviewed text only.** The gate allows PushNotification only with the exact `fallback text:` from a recent failed send. A send that was never reviewed or failed review gets no fallback unless `need` is `blocked`. A withheld push never reads as an allowed fallback, and the marker's `held` field stays separate from `reviewed`.
- **A fallback never lets a claimed hold post too.** The gate releases the hold before allowing the push, and `post_claim` checks cancellation and the delivered record under the lock just before it posts.
- **Every script phone push is a catalog entry** sent through `push_texts.user_send_argv` or `--text-id`. A literal phone send under `scripts/` fails `test_push_texts`. Question review is not a push and needs no catalog entry.
- **The approval file is read only through `review.text_approvals()`.** User-spec approvals carry `objections`. Approving an id keeps records for exactly its current examples. The reviewer's rewrites of the user's own text are never applied; agent-written `nate.adhoc` values take the reviewer's rewrite.
- **Every reader of the `Action:` line** (review.py parse, record and check; send.py; pushover.py) keeps a `Session:` line above it, and the length rule counts that line.
- **Question mode never goes through the push first-line parser** and has no phone headers or length limits.
- **Outer waits come from `review.py` helpers** (`user_send_seconds()` and the others), never from literals. Each reviewer call can take up to `review_timeout_seconds()`, and wherever a rewrite is checked again, the budget allows two reviews.
- **A terminal non-delivery** (`SKIPPED`, `REPLACED`) exits 0. Callers treat a nonzero exit as worth a retry or an alarm.
- **Changes to `questions()` keep calling `record_question_history`** with `question` and `reviewed`, so the nightly count matches the reply. Question detection elsewhere reuses `reply_questions`. Recipient context applies only to the Stop hook, through `user_reply_questions`. `reply_questions` and `sample` stay unchanged.
- **Do not commit `settings.json` or `config/agents.conf` from a unit worktree;** the showrunner applies the exact lines a change proposes.

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
  - ssh wait: timeout + `KILL_GRACE` 10 + 30. ssh `ConnectTimeout=10`.
  - Priority 2 repeats every `RETRY_S` 300 s for up to `EXPIRE_S` 10800 s.
- Live reviews take about 10–13 s each.

**Lengths:**
- The title is under 45 characters and the header plus body is under 500, counted by code. In code the title rule's stem is `push-title-under-45`, which has no rule file; the guide's title rule is `title-names-the-stake`.
- The first 150 characters make sense on their own.
- At most 3 questions.
- The fallback line is at most 200 characters.
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
- Any gate error allows the fallback, so a rare duplicate is still possible.
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

- **PushNotification only as a gated fallback:** the built-in push skips review, so it is allowed only for a failed send and only with that send's reviewed text.
- **A headless `claude -p` with no tools, settings or session:** it reads every message cold, the way the user does, and cannot act. It runs on the unit account with the caller's credentials removed, so it never acts as the sending session.
- **Reviewer faults send the push as written:** an outage must never silence an alert to the user.
- **A failed session push is not sent; registry and emergency pushes are:** a live session can revise and resend. A timer or script has no session to revise in time, and holding its push loses it. So the push goes out and a template fix is requested. The hold code stays only so existing holds finish.
- **Code counts lengths:** the model reported false length failures on short bodies.
- **The time limit lives in `config/notify.conf`:** a fixed 20 s constant was too short and could not be configured, and `config/agents.conf` is pinned.
- **The gate releases the hold before allowing the fallback:** denying the fallback outright while a claim is live could leave the user with no push at all.
- **User-specified texts are approved with recorded objections:** the user's own four lines win over the reviewer's rewrite.
- **No source tracking for example values:** the logs hold no real sends for those senders, so the catalog says where its examples come from instead.
- **No recipient filter in the nightly count:** one day's measurement found 29 of 578 showrunner replies with an unregistered ask. Only one (3.4%) was addressed to a peer only, well under the one-in-four bar, so peer asks are too small a share to matter.
- **No Stop hook for unregistered asks** (user, 2026-10-19: "i have already asked you to get rid of the stop hook that does this"). It once blocked 28 unregistered asks in one day; the nightly count still measures them.
- **One loader for the UX and message guides:** one way to do a thing.
