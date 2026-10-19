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
- `commands/message_style.md` runs the loader on the whole guide and confirms the rule count. `/alert_user`, `/message`, `/message_review`, `/build:report`, `/fix` and the `<Notify>` section of `commands/showrunner/produce.md` load it before their first message to the user in a session.
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
pushover.py send(): parse → action checks (refuse = exit 2) → legacy hold lookup
  → review.review(draft) unless review_off → print "pushover: reviewed JSON: {…}"
  → not sent (exit 6) | post via deliver() → pushover.jsonl
                                              ▼
send.py user(): map exit code → SENT / HELD / REPLACED / REFUSED / FAILED
  → on FAILED or failed-review HELD under a Claude session: write failed/<session_id>.json
                                              ▼
PushNotification (built-in) ── PreToolUse gate ── allowed only with that marker's exact fallback line
```

### The reviewer: `scripts/notify/review.py`

- `review(draft: Draft, revision: bool, *, runner=run_process) -> ReviewOutcome`.
- `Draft(source, priority, title, action: UserAction, body, answerable, session: NotificationSession = NO_SESSION_LINE)`.
- `ReviewOutcome = Approved | Rewritten | Questions | Unreviewed | Failed | Withheld`:
  - `Approved(draft)`
  - `Rewritten(version, failed_rules, …, failures)`
  - `Questions(best_version, questions, failed_rules, …)`
  - `Unreviewed(why)`
  - `Failed(best_version, failed_rules, …)`
  - `Withheld(best_version, why, …)`

  Each outcome also carries `ReviewMetadata(seconds, account, model, binary)`. Each failure is a `RuleFailure(rule, quote)`.
- Each review runs `claude -p` once, through `_review()`:
  - **Binary:** `reviewer_binary()` takes the first executable from `shutil.which("claude")`, `~/.local/bin/claude`, `/etc/profiles/per-user/$USER/bin/claude` or `/run/current-system/sw/bin/claude`. If none is found, the result is `Unreviewed("no claude binary: <paths tried>")`. The `review.jsonl` line records the resolved path or the paths tried.
  - **Account:** `reviewer_environment()` removes the caller's `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `CLAUDE_CONFIG_DIR`, then applies `accounts.prepared_account(accounts.role_account("unit").label)`.
  - **Model:** `agents_resolve_print message_review.reviewer` sets the model, with a 2 s cap on that lookup. When it resolves nothing, the reviewer uses opus at medium effort. `config/agents.conf` has no `message_review` entry today, so the fallback is what runs.
  - **System prompt:** the guide filtered by tag, followed by an instruction. Pushes use `push,all` and `reviewer_prompt.md`. Replies use `reply,all` and an inline prompt. Questions use `question,all` and an inline prompt.
  - **Flags:** `--json-schema SCHEMA --output-format json --tools "" --no-session-persistence --setting-sources ""`.
  - **Time:** the whole call shares one budget of `review_timeout_seconds()`.
- The schema returns `verdict` (`send|rewrite|ask|withhold`), `title`, `action` (string or null), `body`, `questions` (at most 3) and `failed_rules` (a list of `{rule, quote}`).
- `validate()` turns the JSON into an outcome:
  - It removes an `Action: ` prefix from `action`. In push mode it removes a `Session:` line from the body when that line matches the draft's session (a different session makes the result `Unreviewed`). It also removes a first body line that repeats the action line.
  - The action form must not change. A `send` verdict must return the draft unchanged. The title must be ≤ 250 characters and the posted message ≤ 1024. `questions` are allowed only with `ask`. Any violation makes the result `Unreviewed`.
  - Code counts the length rules, `push-title-under-45` and `push-body-under-500`, against both the original and the proposed text. Any length failure the model reports is dropped and replaced by the code count. A failure whose quote is not in the original text is dropped. Each rule counts at most once.
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

- Usage: `pushover.py [--priority 0|1|2] [--source NAME] [--answerable] (--action TEXT | --no-action) TITLE MESSAGE`, or `--text-id ID --value k=v…`, which takes no positional arguments and no action flags.
- With `--text-id`, the push is rendered from the catalog. The template sets the sender, the action and the Session line. The priority comes from the template's `need` (`note`→0, `decision`→1, `blocked`→2), unless `--priority 2` is given.
- Refused before sending (exit 2, logged as `refused before sending: <reason>`):
  - no action stated
  - both `--action` and `--no-action`
  - an empty or placeholder action (`none`, `n/a`, `nothing`, `no action`, `-`)
  - `--no-action` at priority > 0
  - an action longer than the space left in 1024 characters

  This logic lives in `user_action.py`: `parse_user_action`, `refused_at`, `FIX` and `REFUSED_BEFORE_SENDING`.
- `user_action.py` also holds `SessionLine`, `NoSessionLine`, `NotificationSession`, `NO_SESSION_LINE`, `split_session`, `session_prefix` and `first_line(action, session)`. Every reader of the header keeps `Session: …` above `Action: …`.
- Review order: the hold lookup runs first (it is skipped for `--text-id`). Then `review.review()` runs, unless `review_off` exists, in which case the push goes out as written with detail `review: off`. A `Questions` outcome is treated as `Failed`.
- `pushover: reviewed JSON: {title, action, body[, session, text_id, failed_rules, findings, held, sent_despite_failed_review, held_source, held_identity]}` is printed before any post. `send.py` reads it.
- Failed or Withheld:
  - When the priority is 2 or the push has a `--text-id`, it is still sent. A registry push sends its own registered text, not the rewrite. The log detail is `sent despite failed review: <rules>`, and a template-fix request is launched.
  - Otherwise nothing is posted. The command prints `pushover: not sent: <why>` and exits 6.
- `Rewritten` sends the rewrite and prints `pushover: sent as rewritten:`, followed by the title, the header line(s) and the body.
- `deliver(draft, keys_file, post, *, detail)` posts a draft that has already been reviewed. `trimmed_body()` is the only body trim. Keys are read from `~/.config/pushover/env` and are never printed. `post: Post` is injectable, so tests never reach Pushover.

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
| `REPLACED: a newer version replaced this push; nothing was sent` | 0 | pushover 5 (no marker) |
| `REFUSED: <why>` | 2 | pushover 2 with `refused before sending:` (no marker) |
| `FAILED: the user was not reached: <why> fallback text:` + one line, or `… <UNREVIEWED_FALLBACK>` | 3 | Any other failure: exit 1, keys missing, timeout |
| `not sent: probe sessions never push to the phone` | 0 | A session started with `accounts.py exec --probe` |

- `FailedPush(title, action, body, hold, reviewed, need, held)`:
  - `fallback_allowed` is true when `(reviewed and not held) or need == "blocked"`.
  - `text` is the fallback line: `<title> — <header>` with whitespace collapsed (the `Session:` line included when present), at most 200 characters, cut at a word boundary with `…`.
  - `reviewed` is true only when the text came from a `pushover: reviewed JSON:` line.
- `user_failure()` writes the fallback marker `~/.local/state/notify/failed/<session_id>.json` under `failed/.lock`. It does this only when `$CLAUDE_PID` resolves a `sessionId` from `~/.claude/sessions/<pid>.json`, and never on a refusal. The marker holds `time`, `title`, `action`, `body`, `message`, `fallback`, `reviewed`, `need`, `held`, and optionally `held_source`, `held_identity` and `held_machine` (empty means local).
- The wait for a user send is raised to at least `review.user_send_seconds()`.
- `--answerable` is an internal flag, valid only with `--to user`. `send.py` sets it automatically when there is no `--from` and the default sender resolved a session name, and `remote()` forwards it. It is recorded in the `Draft` but no longer changes the outcome.
- Remote (`--machine HOST`): `remote()` runs `CLAUDE_PID= send.py --here …` over `ssh -o BatchMode=yes -o ConnectTimeout=10`, so the far side never writes a marker:
  - On failure, the far side prints `send.py: failed push JSON: {…, reviewed, need, held}` to stderr. The calling side reads that record first, so `reviewed` and `need` survive even when ssh drops stdout. It then writes the marker with `held_machine=<host>`.
  - The header is the first line starting with `SENT|HELD|SKIPPED|QUEUED|FAILED|REFUSED|REPLACED: `. Every later line is kept, and the detail is prefixed `on <host>: `.
- Template-fix requests:
  - `send.py template-fix <json>` stamps `template-fixes/<user-zone date>-<sha256(text_id)>` under a per-id lock, so each text id gets at most one notice per day.
  - It then starts a detached `template-fix-deliver`. That sends `Fix push template <id>` with `--from "push template review"` to `TEMPLATE_FIX_SESSION` from `config/notify.conf`, or to `natedev` when that key is unset.
  - Errors go to `template-fix-errors.jsonl`. There is no retry.

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
2. On a match, the marker is deleted under `failed/.lock`.
3. If `held` is true and `need` is not `blocked`, it denies with `HELD_FALLBACK`.
4. If `reviewed` is false and `need` is not `blocked`, it denies with `UNREVIEWED_FALLBACK`. A marker without `reviewed` counts as reviewed.
5. If `held_source` and `held_identity` are present, it releases the hold, locally or over ssh with `review.py release`. While the answer is `posting`, it asks again each second until `posting_wait_seconds()` has passed. `delivered` denies with `ALREADY_SENT`. Every other answer, and a timeout, allows the push.
6. Otherwise it allows the push. Any exception, including failing to import `review`, also allows the push and prints `push gate: <type>: <error>` to stderr.

### Script push texts

- `scripts/notify/push_texts.py`:
  - `PushText(id, sender, need, title, action: ActionTemplate | NoActionNeeded, body, examples: tuple[PushExample, ...], session: SessionTemplate | NoSessionLine)` and `PushExample(name, values)`.
  - `render(id, **values)` and `render_entry(entry, values) -> RenderedPush`. An unknown id, a missing value or an extra value raises `ValueError`.
  - `user_send_argv(rendered, *, sender, machine, priority, timeout)` builds the `send.py --to user --from … --need … --text-id … --value …` argv. User-send wrappers take a rendered push, never a string.
  - `fingerprint(entry)` is the sha256 over the templates and every example's values and filled text.
  - `REVIEWED_FILE` and `catalog()`, which loads `push_catalog.ENTRIES` and checks for unique ids and named examples.
- `scripts/notify/push_catalog.py` holds every fixed phone text, from scripts and from nixos, each with realistic examples. A comment says where the examples come from. Entry ids include `shutdown.*`, `quota.*`, `rust.*`, `sweep.disk_floor`, `codex.reset_ready` and `nixos.{disk_low,ups_battery,ups_shutdown}`.
- Shutdown holdout texts. `settle.HoldoutKind.from_reason` picks one of two entries:
  - `shutdown.holdout` when a form is shown.
  - `shutdown.holdout_unanswered` for any other hold.

  `shutdown.holdout` uses the user's four lines: title `{session} {operation} is blocked`, `Session: {session}`, the Action line and `Reason: …`. `shutdown.holdout_unanswered` keeps the Session and Action lines, but its title is `Your Claude {operation} is stuck`. Its body says the session has not paused and that stopping it keeps its uncommitted work, then gives the bare command. The user's title and `Reason:` line failed the push review here (reader-starts-cold, title-names-the-stake, short-plain-sentences) and were sent anyway as a registry push, so the showrunner had the text rewritten on 2026-10-16. Both give a stop command, which names the account label (`note_file.canonical_label`) with `--here` and never the login. No raw hold-detail list reaches the push; the requesting session's message keeps it.
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
- **No Stop hook checks a reply** (user, 2026-10-19: "i have already asked you to get rid of the stop hook that does this"). `scripts/hooks/stop-showrunner-footer.py` exits 0 at once and stays only while `settings.json` names it. Registering a question and carrying its reviewed text is the showrunner's rule in `commands/showrunner/produce.md`; the nightly count still reads the history.
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
- The nightly pass proposes at most one new guide rule, through `DIR/messages.md` and the ledger, which leads to `/nightly_next`.

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
- Delegate agents are called "workers", not "seats". Worker naming and its migration are in `docs/as-built/long-lived-workers.md` and `docs/accounts.md`, not here.

## Invariants

- **No test or live check reaches the phone.** Tests fake the reviewer `runner` and `post`. Live checks use `review.py check`, `replay`, `texts` or `sample`, and none of these posts. Probe sessions never push.
- **A push is never lost to the reviewer.** A missing binary, timeout, error, quota or unreadable JSON gives `Unreviewed`, and the push goes out as written, logged `unreviewed: <why>`. Any gate error allows the fallback.
- **The reviewer never adds a fact.** It may cut, reorder and reword. When facts are missing it asks, and a session push that needs facts is not sent.
- **No skip flag.** Only the user's `review_off` switch turns review off.
- **Form is kept.** An action push stays an action push and a no-action push stays one. The Session line stays above `Action:`. Priority never changes. Pushover limits are a title ≤ 250 and a message ≤ 1024, header included.
- **A push that fails review is not sent** (exit 6, `HELD`). Only registry (`--text-id`) and emergency (priority 2) pushes still send, logged with the failed rules. A failing registry text requests a template fix.
- **The fallback is reviewed text only.** The gate allows PushNotification only with the exact `fallback text:` from a recent failed send. A send that was never reviewed or failed review gets no fallback unless `need` is `blocked`. A withheld push never reads as an allowed fallback, and the marker's `held` field stays separate from `reviewed`.
- **A fallback never lets a claimed hold post too.** The gate releases the hold before allowing the push, and `post_claim` checks cancellation and the delivered record under the lock just before it posts.
- **Every script phone push is a catalog entry** sent through `push_texts.user_send_argv` or `--text-id`. A literal phone send under `scripts/` fails `test_push_texts`. Question review is not a push and needs no catalog entry.
- **The approval file is read only through `review.text_approvals()`.** User-spec approvals carry `objections`. Approving an id keeps records for exactly its current examples. The reviewer's rewrites of the user's own text are never applied.
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
