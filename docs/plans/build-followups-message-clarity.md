# Message clarity: every push and every question reaches the user reviewed

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A written message guide, a reviewer that reads every phone push cold before it is sent (send, rewrite, or hold with questions for the writer), a gate on Claude's built-in push, the same review for questions a showrunner asks in a reply, and a nightly pass that grows the guide.

> **As-built disposition: create** — a new `docs/as-built/message-clarity.md`

> **Production: build-followups** — unit `message-clarity-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-13 07:32 EDT

I want you to permanently address the incomprehensibility of this message. When you use the message infra to send a message to the user there needs to be an agent in the mix that reviews such messages for clarity as this one is not at all. That agent needs to act as my advocate for clear communication. Maybe it's a permanent showrunner you relay messages through. Maybe a transient headless session. Maybe it builds a set of style guidelines like /rust_style but for messaging improvement.

The design is `~/.local/state/plan-backlog/2026-10-13-user-message-clarity.md`, option (d), approved by the user 07:4x EDT 2026-10-13 (showrunner's word). Its "Calls I made" stand. The message in question is the 02:19 EDT push "Hana: nested-g2 wrap".

## Delegation Context

- **Project:** `~/.claude` config repo, plus the style repo `~/rust/nate_style`. Worktree `/home/natepiano/worktrees/claude-build-followups-message-clarity`, branch `build-followups-message-clarity` (unit `message-clarity-unit`); name both in every dispatch. Guide files go in the nate_style worktree `/home/natepiano/worktrees/nate_style-message-clarity`, branch `message-guide` (the showrunner merges it to nate_style main with the phase; showrunner's call 2026-10-13).
- **Project started:** 2026-10-13T11:38:57.992+00:00
- **Stack:** Python 3 standard library, checked by basedpyright; zsh; Markdown commands and rule files; `claude -p` for the reviewer.
- **Layout:** `scripts/notify/` (owned: `pushover.py`, `user_action.py`, new `review.py`, `reviewer_prompt.md`, tests) · `scripts/message/send.py` and `commands/message.md` (owner shutdown-unit; also touches) · `commands/alert_user.md`, `commands/showrunner/produce.md` `<Notify>` (hub, owner stalls-unit; also touches) · `docs/user_facing_explanation.md` · new `commands/message_style.md`, `commands/message_review.md` · new `scripts/hooks/pre-tool-use-push-gate.py` · `scripts/production/update_registration.py`, `scripts/hooks/showrunner_footer.py` (Phase 5) · `scripts/nightly_review/nightly_review.py`, `commands/nightly_review.md` (Phase 6) · `~/rust/nate_style/message/` (new). State: `~/.local/state/notify/` (`pushover.jsonl` today; new `review.jsonl`, `held/`, `failed/`, `review_off`).
- **Key files:**
  - `scripts/notify/pushover.py` — `parse` 133, `posted_message` 172, `refuse` 178, `send` 185: parses, checks the action (`parse_user_action`, `refused_at`), builds `message = posted_message(...)` (the `Action: …` / `No action needed.` first line + body), reads keys, posts, logs to `LOG_FILE` (`pushover.jsonl`). Limits `TITLE_MAX` 250, `MESSAGE_MAX` 1024. Exit 0 sent, 1 Pushover refused/unreachable, 2 refused before sending/usage/keys. `post: Post` is injectable for tests.
  - `scripts/notify/user_action.py` — `ActionRequired(text)`, `NoActionRequired`, `parse_user_action`, `refused_at`, `first_line`, `REFUSED_BEFORE_SENDING`, `FIX`.
  - `scripts/notify/test_pushover.py` — fakes `post` and the keys file; no test reaches Pushover.
  - `scripts/message/send.py` — `USER_CHANNEL` 92 (pushover.py), `one_line` 691 (truncates to 300), `user()` 830 (runs pushover with `--priority`, `--source message.sender`, action flags, summary, text; maps exit 0 → `sent`, exit 2 with `refused before sending:` → `refused`, else `failed`), `remote()` 843 (forwards a `--to user` send to `--machine` over ssh; parses the last line's `SENT|SKIPPED|QUEUED|FAILED|REFUSED` word), `default_sender` (session name from `~/.claude/sessions/$CLAUDE_PID.json`, else `send.py`). Scripts pass `--from <script name>`; sessions send without `--from`.
  - `scripts/message/escalate.py` — `schedule()` 279: the pattern for a notifier job (`zsh notifier.sh new <job> --every 1 --run "<py> <script> due"`; a repeated `new` changes nothing).
  - `scripts/accounts/accounts.py` — `role_account("unit")` 1056 → `DefaultAccount` or `NamedAccount(label, dir)`; `scripts/whoami/account.py` `named_account_environment(dir)` 126 gives the env (`CLAUDE_CONFIG_DIR` and the memory base). `scripts/nightly_review/nightly_review.py` 180 shows the import pattern.
  - `scripts/agents/agents_config.sh` — `agents_resolve_print <function>.<subtask>` prints `task=… family=… agent=<model> effort=<effort> tier=…`.
  - `scripts/ux_style/load-ux-style.sh` — loads a folder of rule files (`--guide <dir>`): each `.md` with `test:` in its frontmatter is a rule; prints every rule body, then `=== UX_CHECKLIST ===` and one `stem — title — test` line per rule; `--checklist` prints only the lines. Reused unchanged for the message guide (unit director's call: no second loader; the checklist header's name is cosmetic).
  - `~/rust/hanadocs/ux/assemblies-open-show-and-close-clearly.md` — the rule-file shape to copy: frontmatter `date_created`, `date_modified`, `tags: [...]`, `mechanism: llm`, `test: "<question>"`, `source: "<the user's words, date>"`; body `## Title`, a paragraph, `### Bad`, `### Good`.
  - `docs/user_facing_explanation.md` — the decision-explanation method `/adhoc_review` and `/unit:direct` load; its "banned vocabulary" and "behavior before name" rules are guide sources.
  - Source memories for the guide: `~/.claude/projects/-etc-nixos/memory/{answer-in-a-few-lines,name-the-time-zone,open-questions-as-a-count,phone-push-must-be-actionable}.md`, `~/.claude/projects/-home-natepiano-rust/memory/speak-plainly.md`, `~/.claude/projects/-home-natepiano-rust-hana/memory/{speak-simply-and-briefly,explain-means-explain}.md`, `~/.claude/projects/-home-natepiano-rust-cargo-liner/memory/hooks-speak-only-when-actionable.md`.
  - `commands/showrunner/produce.md` `<Notify>` 929–964: the send line with the fixed `--summary "Hana: <unit or topic>"` prefix, the 200-character rule, the PushNotification fallback, the Need table.
  - `commands/alert_user.md` — the alert send line and fallback.
- **Test lanes:** none (no `tests/` directories); tests sit beside the code as `test_*.py` and import their sibling by bare name.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s scripts/notify -p 'test_*.py'` and `python3 -m unittest discover -s scripts/message -p 'test_*.py'`; Phase 5 adds `-s scripts/production` and `-s scripts/hooks`; Phase 6 adds `-s scripts/nightly_review`.
- **Lint:** `basedpyright <changed .py files>` — pass is its `0 errors, 0 warnings` line (it exits 3 on the missing `.venv`).
- **Invariants:**
  - **No test or live check reaches the phone** (user rule, memory `phone-push-must-be-actionable`; showrunner 2026-10-13). Tests fake the reviewer process and `post`; a live check uses `review.py replay` or `--dry-run`, which never posts.
  - **A push is never lost.** Reviewer missing, slower than its configured limit (`REVIEW_TIMEOUT_SECONDS`, default 60 s), failing, out of quota, or returning unreadable JSON → the push goes out as written and the log marks it `unreviewed: <why>` (approved design).
  - **The reviewer never adds a fact.** It may cut, reorder and reword; whatever is missing it asks for (approved design).
  - **No skip option.** `pushover.py` has no flag that bypasses review; only the user's switch (`review_off`) turns it off (approved design).
  - **The reviewer keeps the action form and priority.** An `--action` push stays an action push (non-empty action text); a `--no-action` push stays one; priority never changes.
  - Pushover limits: title ≤ 250, message ≤ 1024 including the first line.
  - Do not commit `settings.json` or `config/agents.conf`: the showrunner applies the exact proposed lines from the phase notice (showrunner's call 2026-10-13).
  - Python: basedpyright 0/0 on changed files; no file-level ignores; `TypedDict` for every JSON shape; no `Any`.

## Phases

### Phase 1 — The message guide and /message_style  · status: done

#### As-built

- The message guide is 13 rules in `~/rust/nate_style/message/`, one per file in the ux rule-file shape, on nate_style main (4aaf1f1). Each rule carries `tags` from `all`, `push`, `question`, `reply`, a `source` quoting the user's words with their date, a yes/no `test` a cold reader answers from the message alone, and a `### Bad` / `### Good` pair.
- Rule stems: `reader-starts-cold`, `what-happened-why-what-you-do`, `every-choice-says-what-it-does`, `say-where-to-act`, `title-names-the-stake`, `first-150-stand-alone`, `push-body-under-500`, `times-carry-zone`, `push-only-what-the-user-must-do`, `short-plain-sentences`, `before-and-after`, `ask-once-then-count` (`question`, `reply`), `explain-means-explain` (`reply`).
- `/message_style` runs `zsh ~/.claude/scripts/ux_style/load-ux-style.sh --guide ~/rust/nate_style/message`, reads the output whole and confirms the rule count. `--tags push,all` selects 10 rules.
- `/alert_user`, `/message`, produce.md `<Notify>`, `/build:report` and `/fix` load `/message_style` before their first message to the user in a session.
- A push title is under 45 characters and names the stake, with no fixed prefix (`/build:report`'s title is "Review watch waits on your acknowledgment"); its first 150 characters stand alone; its body is under 500 characters.

**Files:**
- `~/rust/nate_style/message/` — the 13 rule files and a README (no `test:`, so the loader skips it)
- `~/rust/nate_style/README.md` — a `message/` line under Structure
- `commands/message_style.md` — loads the guide and confirms the rule count
- `commands/alert_user.md`, `commands/message.md`, `commands/build/report.md`, `commands/fix.md` — the loader line; stake titles
- `commands/showrunner/produce.md` — `<Notify>`: loader line, `--summary "<what is at stake, under 45 characters>"`, body under 500 characters
- `docs/user_facing_explanation.md` — points wording to `/message_style`; its decision method is unchanged

**Binds later work:** the guide path `~/rust/nate_style/message/` and its rule stems; the loader `load-ux-style.sh --guide` with its `--tags` filter; the tag set `all`/`push`/`question`/`reply`. The message reviewer and send.py's review outcomes read these.

**Gotchas:** a push's `Action:` / `No action needed.` line always comes first, and every order rule applies after it. `ask-once-then-count` limits only the end-of-reply reminder, never an answer the user asked for.

### Phase 2 — The push reviewer  · status: done

#### As-built

- `scripts/notify/review.py` reviews every phone push before `pushover.send()` posts it (after the action checks, before the keys are read). `review(draft, revision, *, runner=run_process) -> ReviewOutcome` runs `claude -p` on the unit account (the caller's `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `CLAUDE_CONFIG_DIR` removed first) with the guide's `push,all` rules plus `reviewer_prompt.md` as the system prompt, a JSON schema, no tools, no settings and no session, all inside a shared 20 s budget. Model and effort come from `agents_resolve_print message_review.reviewer` and fall back to opus / medium. Live reviews take 10–13 s.
- `ReviewOutcome = Approved(draft) | Rewritten(version, failed_rules) | Questions(best_version, questions, failed_rules) | Unreviewed(why)`. `validate` enforces: title ≤ 250, posted message ≤ 1024, ≤ 3 questions only on `ask`, the action form unchanged, and `send` meaning unchanged. On a revision, `ask` becomes `Rewritten`. It first drops a body line that repeats the action line, and an `Action: ` prefix on the action. Any fault, of any exception type, is `Unreviewed` and the push goes out as written.
- `pushover.py`: `--answerable`; exit 4 when held. A rewrite prints `pushover: sent as rewritten:` and then the title, action line and body. A script's failed text (`ask` without `--answerable`) posts the best version at once and logs `rewritten, script text failed: …`. The `review_off` switch sends as written. `deliver()` posts an already reviewed `Draft`. `trimmed_body()` is the one body trim.
- Holds: `held/<sha256(source)[:16]>.json` with `HoldLookup = NoHold | LiveHold(Held) | UnreadableHold`; an unreadable file moves to `held/bad/`. A claim is a stamped rename to `.sending` under `review.lock`. A claim older than `CLAIM_STALE_SECONDS` (120 s) is dead and is taken over by `due` or a revision. `review.py due` (notifier job `notify-review`, every minute) posts expired holds one at a time and restores any that fail, logging `expired send failed: …`.
- Commands: `review.py replay [--limit N]`, `check --title T (--action A | --no-action) --text B [--priority N]` (dry), `switch on|off|status`; `/message_review`. Log `~/.local/state/notify/review.jsonl`. `quota_alert.py` and `rust_release.py` wait 60 s on their user send. Guide rule `every-choice-says-what-it-does` carries the `push` tag (11 push rules).

**Files:**
- `scripts/notify/review.py`, `reviewer_prompt.md`, `test_review.py` — the reviewer, holds, commands
- `scripts/notify/pushover.py`, `test_pushover.py` — the review step, `--answerable`, exit 4, `deliver()`
- `commands/message_review.md` — the on/off switch
- `scripts/whoami/quota_alert.py`, `scripts/buildlog/rust_release.py` and their tests — 60 s waits

**Binds later work:** pushover exit 4 and the `pushover: sent as rewritten:` stdout block are the contract `send.py` parses. A revision fits review 20 s plus post 15 s inside `send.py`'s 40 s wait. `review.py check` is the dry review a gate uses. The proposed `config/agents.conf` lines (`message_review=claude`, `[message_review.claude] reviewer=opus:medium`) are the showrunner's to add.

**Gotchas:** without an explicit instruction, the reviewer repeats the action line at the top of its body; `validate` strips it. A sender killed mid-post can cause one repeat post after 120 s, because the remote accept and the local release cannot be atomic.

### Phase 3 — send.py's review outcomes, the built-in push gate and every script's fixed push text  · status: done

#### As-built

- `send.py` passes `--answerable` to `pushover.py` when the send has no `--from` and `default_sender()` resolved a session name; `--answerable` is an internal flag (only with `--to user`), and `remote()` forwards it. Outcomes for `--to user`: pushover exit 4 → `HELD: pushover: held for review: …` plus the numbered questions (exit 4); exit 0 with `pushover: sent as rewritten:` → `SENT: to the user, rewritten:` followed by the sent title, action and body lines, untruncated; refusal (exit 2 with `refused before sending:`) → `REFUSED`; any other failure (exit 1, exit 2 for missing keys, timeout) → `FAILED: the user was not reached: <why> fallback text:` and one line.
- The fallback line is `FailedPush.text`: `<title> — <action line>`, whitespace-collapsed, at most 200 characters, cut at a word boundary with `…`. Built-in PushNotification takes only `message` (one line under 200 characters, no title), so the gate compares that one line.
- `pushover.py` prints `pushover: reviewed JSON: {title, action, body[, held_source, held_identity]}` before posting; `held_*` appear when a revision claimed a hold, with `held_identity = review.hold_identity(hold)` (sha256 of the hold record). `send.py` parses it into `FailedPush`, so the fallback carries the reviewed text.
- `remote()` takes the first `SENT|HELD|SKIPPED|QUEUED|FAILED|REFUSED: ` line of the ssh output as the header and keeps every line after it, prefixed `on <host>: `. The far side runs with `CLAUDE_PID=` empty, so it never writes a marker; on `FAILED` it prints `send.py: failed push JSON: …` to stderr, and the originating `send.py` writes the marker with `held_machine=<host>`.
- `scripts/hooks/pre-tool-use-push-gate.py` (PreToolUse, matcher `PushNotification`) allows only when `failed/<session_id>.json` is under 600 s old and the push's `message` equals the marker's `fallback` (whitespace-normalised); it then releases the named hold and deletes the marker, under `failed/.lock`. Otherwise it denies with `PushNotification is only the fallback for a failed send. Send with ~/.claude/scripts/message/send.py --to user (see /alert_user); it is reviewed for clarity first.` Any exception allows and prints `push gate: <type>: <error>` to stderr.
- Script push texts pass the guide's `push` and `all` checklists, and every script sends with `--from <script name>`, so none is held. Quota status notes that ask nothing of the user go to the quota log (`quota status <name>: …`), not the phone.

**Files:**
- `scripts/message/send.py` — `HELD`, rewritten `SENT`, `--answerable`, `FailedPush`, `user_failure()` marker writer, remote header parsing
- `scripts/notify/pushover.py` — `pushover: reviewed JSON:` line with the claimed hold's source and identity
- `scripts/notify/review.py` — `hold_identity(held)`, `release_if_matches(source, identity)`, `review.py release --identity <id> -- <source>`
- `scripts/hooks/pre-tool-use-push-gate.py` / `test_push_gate.py` — the gate and its tests
- `commands/message.md` — `HELD` and rewritten `SENT` outcomes; `commands/{alert_user,build/report,fix,watcher}.md`, `commands/showrunner/produce.md` `<Notify>` — a failed send to the user falls back to PushNotification with the printed `fallback text:`; `/message` keeps no fallback between sessions
- `scripts/buildlog/rust_release.py`, `scripts/lint/sweep.py`, `scripts/production/{ci_points,codex_winddown}.py`, `scripts/shutdown/{restart,settle}.py`, `scripts/whoami/quota_alert.py` and their tests — fixed push texts; `shutdown/record.py`, `shutdown/shutdown.py` and `message/escalate.py` already passed

**Binds later work:** The marker `~/.local/state/notify/failed/<session_id>.json` (`NOTIFY_STATE_DIR` overrides the base) holds `time`, `title`, `action`, `body`, `message`, `fallback`, and optional `held_source`, `held_identity` (`review.hold_identity`) and `held_machine` (empty = local). It is written by `send.py` only under a Claude session (`~/.claude/sessions/$CLAUDE_PID.json` → `sessionId`) on every user-delivery failure, never on a refusal. On a matching fallback the gate releases the hold before consuming the marker: locally via `review.release_if_matches(source, identity)`, or `ssh -o BatchMode=yes -o ConnectTimeout=10 <machine> '~/.claude/scripts/lib/py ~/.claude/scripts/notify/review.py release --identity <id> -- <source>'`; any gate error allows. `release_if_matches` reads only `held/<id>.json`, so a claimed `.sending` hold is not released and can still post after the fallback — the phase "A fallback push never lets a claimed hold post too, and the reviewer passes its own rewrite" owns that. The hook stays inactive until the showrunner adds the `settings.json` PreToolUse entry `{"matcher": "PushNotification", "hooks": [{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/pre-tool-use-push-gate.py\""}]}`.

**Gotchas:** The live reviewer is not stable on its own rewrite (2 of 4 rewrites re-passed; one flipped to `ask`, one kept failing push-body-under-500 on about 330 characters), so `review.py check` over the script texts gives `send` for 6 of 15 and a reword-only `rewrite` for 9, never `ask`. Work that builds on `review.review` (questions, the nightly sample) sees that noise until the reviewer is stable.

**Ruled out:** Denying the fallback outright while a claim is live — the user could get no push at all.

### Phase 4 — A fallback push never lets a claimed hold post too, and the reviewer passes its own rewrite  · status: done

#### As-built

- **Hold lifecycle** (`scripts/notify/review.py`): a claim is a stamped rename of `held/<id>.json` to `<id>.sending`; `post_claim(held, send) -> ClaimSent | ClaimCancelled | ClaimFailed(code)` re-checks under the review lock, immediately before posting, that the claim still exists and has no cancel mark `held/<id>.cancel` for its identity, drops a cancelled claim unposted, and writes the delivered record `held/delivered/<identity>` (kept `DELIVERED_KEEP_SECONDS = 3600`) before releasing the lock. `due()` and a revision's post both go through it.
- `release_if_matches(source, identity, *, timeout, clock, sleep) -> ReleaseOutcome` answers under the lock: `delivered` (record exists), `released` (a matching `.json`, or a matching claim older than the stale limit, is deleted and a cancel mark written), `posting` (a matching younger claim), or `none` (no hold, or one with another identity, left alone). `review.py release --identity I -- SOURCE` prints that word on one line.
- **Push gate** (`scripts/hooks/pre-tool-use-push-gate.py`): consumes the `failed/<session_id>.json` marker in every case, releases locally or reads the word from the ssh output; `delivered` denies with `ALREADY_SENT` ("The held version of this push was already sent."), `released`/`none` allow, `posting` re-asks each second up to `posting_wait_seconds()` then follows the answer, allowing on timeout. Any gate error allows the fallback, so a rare duplicate remains possible.
- **Replaced push:** `pushover.py` posts a revision through `post_claim`; a cancelled claim exits 5, and `send.py` reports `REPLACED: why` with exit 0 like `SKIPPED`, locally and remotely, with no fallback marker.
- **Time limit:** `config/notify.conf` holds `REVIEW_TIMEOUT_SECONDS=60`; `review_timeout_seconds()` reads it once per call (`NOTIFY_CONFIG` names the path) and falls back to 60 with a stderr line on a missing file, key or bad value. Every wait derives from it in `review.py`: `revision_seconds()` = R+15, `user_send_seconds()` = R+25, `claim_stale_seconds()` = max(120, R+25), `posting_wait_seconds()` = min(110, R+20). The settings hook entry for PushNotification carries `"timeout": 120`.
- **Reviewer:** length rules (title under 45, action plus body under 500, plus the 250/1024 transport limits) are counted by `review.validate` against both the original and proposed text; a false length failure from the model is removed and other failures kept. Each failure is `{rule, quote}` and a quote absent from the text is rejected. `reviewer_prompt.md` returns a text that meets every rule unchanged with `send`.
- `quota_alert.py` has two delivery roles: a phone push with its ask, and a status note that asks nothing, logged only.

**Files:**
- `config/notify.conf` — reviewer time limit
- `scripts/notify/review.py` — hold lifecycle, release outcomes, time limit and derived waits, length counts
- `scripts/notify/reviewer_prompt.md` — unchanged-text and quoted-failure rules
- `scripts/hooks/pre-tool-use-push-gate.py` — release before fallback, `posting` wait
- `scripts/notify/pushover.py`, `scripts/message/send.py`, `commands/message.md` — `REPLACED` outcome; `send.py`'s user-delivery wait
- `scripts/whoami/quota_alert.py`, `scripts/buildlog/rust_release.py`, `scripts/lint/sweep.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py` — waits from `user_send_seconds()`, fixed push texts

**Binds later work:** Outer waits around a reviewed send or question review derive from `review.py` helpers, never literals; each reviewer call can take up to `review_timeout_seconds()`, so a batch of sample reviews in the nightly scope budgets that per reply.

**Gotchas:**
- Modules that other packages import as libraries import `scripts/notify/review` lazily: `test_update_registration` copies the command tree to a temp dir without `scripts/notify`, so `sweep.py` imports it inside `send_floor_alert` and modules that only read sweep's config never load it.
- A terminal non-delivery outcome (`SKIPPED`, `REPLACED`) exits 0; callers treat nonzero as retry-worthy and alarm on it.
- The gate's `posting` cap of 110 s keeps the hook inside its 120 s settings timeout (Claude Code's default is 60 s).

**Ruled out:** a fixed reviewer constant (`TIMEOUT_S = 20`), too short and not configurable; storing the limit in `config/agents.conf`, which is pinned; letting the model judge length limits, which it flagged falsely on short bodies.

### Phase 8 — Every push to the user goes through the review: no unreviewed path  · status: done

#### As-built

- `review.reviewer_binary()` resolves the reviewer once per review: `shutil.which("claude")`, then `~/.local/bin/claude`, `/etc/profiles/per-user/$USER/bin/claude`, `/run/current-system/sw/bin/claude`; the first executable wins. None found gives `Unreviewed("no claude binary: <paths tried>")`, and the push still goes out as written. The `review.jsonl` line records the resolved path or the paths tried.
- `send.py`'s `FailedPush` and the fallback marker `failed/<session_id>.json` carry `reviewed: bool` (true only when the text came from a `pushover: reviewed JSON:` line, local or remote) and `need: "note" | "decision" | "blocked"`. A remote failure reads the stderr `send.py: failed push JSON:` record first, so both fields survive when an SSH drop loses the stdout outcome header.
- `pre-tool-use-push-gate.py` allows a fallback only when the marker says `reviewed: true` or `need: "blocked"`; otherwise it consumes the marker and denies with `UNREVIEWED_FALLBACK` ("This push was never reviewed, so it cannot go out as a fallback. Bring it to the user in your reply, or resend it with send.py once the failure is fixed."). A marker without the fields is treated as reviewed. Any gate error still allows.
- `send.py`'s `FAILED:` line carries the same denial when the fallback would be denied; `commands/message.md` states when `FAILED` permits a fallback.

**Files:**
- `scripts/notify/review.py` — `reviewer_binary()` and its log fields
- `scripts/message/send.py` — `FailedPush.reviewed`/`need`, the marker fields, remote stderr record read first, the `FAILED:` line
- `scripts/hooks/pre-tool-use-push-gate.py` — the unreviewed non-blocked fallback denial
- `commands/message.md` — the `FAILED` fallback rule
- `scripts/notify/test_review.py`, `scripts/message/test_send.py`, `scripts/hooks/test_push_gate.py`, `scripts/notify/test_pushover.py` — their cases

**Binds later work:** the push-text registry's `Withheld`/`Failed` outcomes keep the marker's `reviewed` and `need` and the gate rule that an unreviewed non-blocked failure denies the fallback; a withheld push never reads as a permitted fallback, and its `held` marker field stays separate from `reviewed`. `Unreviewed` still sends.

**Gotchas:** test fakes of the reviewer match the resolved binary by basename (`Path(argv[0]).name == "claude"`), since the resolved path is absolute; a bare `argv[0] == "claude"` fake never matches.

**Ruled out:** holding a script or timer push at send time for a root-showrunner report — a timer has no session to report to, and holding loses the push.

### Phase 9 — Script pushes are reviewed before they ship, and a push that fails review is not sent  · status: done

#### As-built

- `scripts/notify/push_texts.py` is the one way a script builds a phone push: `PushText` (templates `sender`, `need`, `title`, `action`, `body`; `action` is an `ActionTemplate` or no action, never `str | None`), `PushExample`, `ActionTemplate`, `render(id, **values)` (an unknown id or a missing or extra value raises), `render_entry`, `fingerprint`, `user_send_argv`, `REVIEWED_FILE`; `catalog()` loads `push_catalog.ENTRIES`. User-send wrappers take a rendered push, never a string.
- `scripts/notify/push_catalog.py` holds every fixed phone text, script and nixos, each with realistic examples; the catalog says where its examples come from.
- `review.py texts [--id X] [--approve]` dry-reviews every filled example with the live reviewer and posts nothing; `--approve` records an approval in `scripts/notify/push_texts_reviewed.json`, keyed by a template fingerprint over the templates and every example's values and filled text.
- `test_push_texts.py` gates: every catalog example has a current approval; title < 45 and `"Action: …\n" + body` < 500; a literal phone send (`--to user`, `--to=user`, a `pushover.py` path, `broadcast.send("user", …)`) under `scripts/` outside `push_texts.py`, `pushover.py`, `send.py`, `escalate.py` fails. `pre-tool-use-push-gate.py` applies the same token rule.
- At send time a rewrite is re-checked once; a second failure is `ReviewOutcome` `Failed`, and a push needing nothing from the user is `Withheld` (verdict `withhold`, push mode only). A push that fails review is not sent (pushover exit 6) and the sending session is told so it can revise and resend; registry and emergency (`--need blocked`) pushes still send, logged with the failed rules, and a failing registry text schedules a template-fix request. `Unreviewed` (reviewer down) still sends as written.
- nixos senders call `pushover.py --text-id nixos.disk_low` (values `free`, `shortfall`), `nixos.ups_battery` (`charge`, `minutes`), `nixos.ups_shutdown` (no values).
- Status-only pushes go to the log or the requesting session; the five-hour warning is not a phone push. `quota.claude` names the target account and its remaining usage (`target`, `target_remaining`). Shutdown stop/restart failures carry a counted `stopped` value ("One session" / "N sessions"); `shutdown.form` takes `machine` and `session`.

**Files:**
- `scripts/notify/push_texts.py` — render and approval API
- `scripts/notify/push_catalog.py` — the text catalog
- `scripts/notify/push_texts_reviewed.json` — approvals by fingerprint
- `scripts/notify/test_push_texts.py` — approval, length and token gates
- `scripts/notify/review.py` — `Failed`, `Withheld`, rewrite re-check, two-review budget, `texts`
- `scripts/notify/pushover.py`, `scripts/message/send.py` — exit 6, `--text-id`/`--value`, sends despite failure for registry and blocked pushes
- `scripts/hooks/pre-tool-use-push-gate.py` — the token rule

**Binds later work:** a new script phone push is a catalog entry sent through `push_texts.user_send_argv`; `test_push_texts` fails a literal phone send anywhere under `scripts/`. Question review is not a push and needs no catalog entry. `review.py` holds the `texts` subcommand, so edits to it keep `test_push_texts` and `test_review` green.

**Gotchas:**
- Changing a template or an example value invalidates the approval; rerun `review.py texts --id <id>` and `--approve`.
- The reviewer is not deterministic: approve only after three passing runs.
- Example values decide the verdict as much as templates: unrealistic or contradictory names fail review.
- `delivery_waits()` budgets two reviews wherever a rewrite is re-checked (sender waits and `claim_stale` included).

**Ruled out:** provenance tracking of example values against real sends — the logs hold no real sends for those senders; the catalog says where its examples come from.

### Phase 10 — Workers, not seats: one word for a delegate agent everywhere  · status: done

#### As-built

Outside `scripts/accounts/` and `scripts/whoami/`, every delegate agent is a "worker" in prose, CLI names, identifiers, files and tests, with no old-name alias. Renamed: `remove_workers.py`, `worker_name.sh`, the `**Workers:**` Work Order field (format docs, `berth.work_order`), `<LongLivedWorkers/>`, `worker:` message addresses, `workers-baseline`, buildlog schema 16, `feature_log --worker`, and `docs/as-built/long-lived-workers.md`. Old-name persisted state (`impl_seat_<slot>.json`, `<run>/seats`, `impl_seat_claim.lock`, `seats-baseline`, buildlog and feature_log seat fields, shutdown `busy_seats` and progress labels) converts to the new name on first read; after that, readers see only new names.

**Files:**
- `scripts/delegate/worker_state.py` — first-read conversion of session files and the run ledger; `_link_live_name` hard-links the worker name to the live file
- `scripts/delegate/remove_workers.py`, `worker_name.sh` — renamed from the seat names
- `scripts/shutdown/record.py` — record conversion, progress labels included
- `scripts/agents/`, `scripts/usage/`, `scripts/buildlog/`, `scripts/feature_log/`, `scripts/message/`, `scripts/production/` — identifiers and stored fields
- `commands/`, `docs/`, `config/delegate.conf`, `codex/AGENTS-messaging.md` — wording

**Binds later work:** these spellings stay because code in `scripts/accounts/` or `scripts/whoami/` reads them, and the accounts and whoami rename renames each with its readers (and a first-read migration for stored ones): shutdown inventory kind `seat` and record field `seats`, the moves-journal subject "seats" and field `seat`, the accounts `seats` role and CLI subcommands, `seat_usage.py`, the `day1_seats` fixture, `CodexSeatsAccount`, `seats_home`, `retire_seats_servers`.

**Gotchas:** live state files (`<run>/seats`, `impl_seat_claim.lock`) are never renamed in place: the worker name is a hard link to the same inode and the old name stays, so a launcher started before the update keeps appending to and locking the same file. The `seats-baseline` → `workers-baseline` conversion tolerates a concurrent rename.

**Ruled out:** renaming live state files in place — it races running launchers; a CLI or identifier alias for old names — one way to do a thing.

### Phase 11 — Workers, not seats, in the accounts and whoami scripts  · status: todo

**Depends on:** Phase 10

#### Work Order

**Goal:** The two directories still held during the rename and every name they define say "worker", with no alias, and `git grep -i seat` finds nothing outside the allowed remainder.

**Spec:**
- Runs once the showrunner says two-accounts has released `scripts/accounts/` and `scripts/whoami/` (`scripts/delegate/` moved into the phase before when builder-report released it).
- Applies the rename Spec of the phase before it to `scripts/accounts/` and `scripts/whoami/`, and renames every name they define or read together with each reference elsewhere: `seat_usage.py` → `worker_usage.py` (+ test), `accounts.py swap|set-role|role seats` → `workers` (no alias), `scripts/usage/fixtures/day1_seats/` → `day1_workers/`, identifiers, flags, JSON keys and state file names. Before domain validation, convert account configuration `seats` to `workers`, role-history `role: "seats"` to `"workers"`, moves-journal `subject: "seats"` to `"workers"` and confirmed-move `seat` to `worker`, and shutdown record `seats` with nested `kind: "seat"` to their worker names. accounts and whoami share the configuration conversion, so neither falls back to the default home on an old file (`whoami/agent_accounts.py` does today when the key is absent). Preserve account selection, move ids, timestamps, worker and run identity, confirmation deduplication (`accounts/moves.py` compares `subject`, `seat`, `run`), and shutdown ownership and progress. Journal conversion holds the appenders' lock on the same inode, keeps the reader's malformed-line contract, and reads an old-format entry appended later by a process started before the update. Canonical reads return only worker names. The allowed `git grep -i seat` remainder after this phase is legacy input names in migration code and its tests, plus the `**Seats:**` validator fixtures.
- Merge origin/build-followups first: two-accounts' work in these directories lands before this phase.

**Files:**
- `scripts/accounts/` — account roles, configuration, role history, moves journal, inventory consumers and tests
- `scripts/whoami/` — worker account resolution, quota transitions, `worker_usage.py` and tests
- `scripts/agents/` — account imports (`agent_exec.sh` imports `seats_home`), move-journal producers (`codex_mesh.py`), `retire_seats_servers` and tests
- `scripts/message/` — account imports (`escalate.py`), role descriptions and tests
- `scripts/shutdown/` — inventory `SeatSession` / `kind: "seat"`, record field `seats`, `stop.MatchingSession.seats`, migration, callers and tests
- `scripts/production/` — account imports and shutdown-record fixtures in tests
- `scripts/delegate/` — `review_pool_report.py`'s `seat_usage` import and related tests
- `scripts/usage/` — the day1 fixture directory, contents and callers
- `commands/` — references to the renamed names
- `docs/` — references to the renamed names, outside the plan docs

**Workers:** 2 writers (no test lane)
- `impl` — agents, message, shutdown, production, delegate, usage, commands and docs above
- `test` (opens as impl) — `scripts/accounts/`, `scripts/whoami/`; publishes the renamed account and move-journal names before `impl` updates their consumers

**Constraints from prior phases:** Rename together, with no old API or CLI alias: `CodexSeatsAccount`, `seats_home`, `retire_seats_servers`, `SwapSeatSession`, `SeatsSwapDryRun`, `SeatsSwapStarted`, `SeatsSwapOutcome`, `SeatsTransitionDelivery`, `CodexAlertPassReadings.seats` and their seat-spelled helpers and constants; `accounts.py env|swap|set-role|role seats`; shutdown `SeatSession`, inventory `kind: "seat"`, `SessionShutdown.seats`, `MatchingSession.seats`; moves-journal `subject: "seats"` and confirmed-move `seat`. Phase 10's ledger and claim-lock conversion stays as built: old `<run>/seats` and `impl_seat_claim.lock` paths remain linked to the worker files for launchers started before the update (`scripts/delegate/worker_state.py`). Run every script suite before checkpoint, the lib suite and `scripts/notify/test_push_texts.py` included. A changed `push_catalog.py` text needs `review.py texts --id <id>` three times and `--approve`.

**Acceptance gate:** every script suite green; basedpyright 0/0; `accounts.py swap workers` works and `swap seats` is unknown; `git grep -i seat` reports only the allowed remainder; tests load an old selected-account configuration through both accounts and whoami and keep the selected home; read old role history without changing burn figures; resume an old requested or waited move and deduplicate its confirmation after conversion; see a later old-format journal append; load old shutdown records with nested worker sessions keeping ownership, progress and issues; a second unchanged read rewrites nothing; Phase 10's shared-ledger and claim-lock tests stay green.

### Phase 12 — Shutdown alerts in the user's four-line form; the forms command docs  · status: done

#### As-built

- A push can carry a Session line: `PushText.session: SessionTemplate | NoSessionLine` (`push_texts.py`). The delivered message reads `Session: …\nAction: …\n<body>`; entries without one render byte for byte as before, and the length rule counts the Session line.
- `user_action.py` holds `SessionLine`, `NoSessionLine`, `NotificationSession` (their union), `NO_SESSION_LINE`, `split_session`, `session_prefix` and `first_line(action, session)`. `review.py` (parse, record and direct `check`), `send.py` and `pushover.py` keep a `Session:` line above the leading `Action:` line.
- `shutdown.holdout` is the user's spec: Title `{session} {operation} is blocked`; Session `{session}`; Action `answer the open form, or run the command below to stop {session}`; body `Reason: {operation} is blocked because {session}'s claude session is not answering, and it can't proceed.`, then Stop now (recommended), `Do nothing: the {request} keeps waiting. It has waited {elapsed} so far.`, and `Command: {command} (on {machine})`. Its render values are `operation`, `request`, `session`, `machine`, `elapsed`, `after_stop`, `command`; `host` is gone from the template and the `settle.py` call site.
- `commands/agent_send.md`, `idle_timeout.md` and `issue.md` ask in one plain-text line with a recommendation; `issue.md`'s rating survey is answered with five star counts. `git grep AskUserQuestion -- commands` finds only lines that forbid it; shutdown's form detection (`SHOWING_FORM`, check-form) stays as the backstop.

**Files:**
- `scripts/notify/push_texts.py` — `SessionTemplate`, render with the Session line, length rule
- `scripts/notify/user_action.py` — Session-line types and split
- `scripts/notify/review.py`, `scripts/notify/pushover.py`, `scripts/message/send.py` — keep the Session line above Action
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — holdout entry and its user-spec approval
- `scripts/shutdown/settle.py` — holdout render call without `host`
- `commands/agent_send.md`, `commands/idle_timeout.md`, `commands/issue.md` — plain-text questions

**Binds later work:** `PushText.session: SessionTemplate | NoSessionLine`; `user_action.NotificationSession`, `NO_SESSION_LINE`, `split_session`, `first_line(action, session)`. Every reader of the leading `Action:` line (review.py parse/record/check, send.py, pushover.py) keeps a `Session:` line above it, and the length test counts the Session line. A user-spec text's approval entry in `push_texts_reviewed.json` uses `push_texts.fingerprint(entry)` with a reviewer string naming the user spec. The `shutdown.remote_control` entry is unchanged here; the remote-control alert gives it a Session line. A question mode in the review.py engine must not route a question through the push first-line parser.

**Gotchas:**
- `review.py texts --approve` writes only passing texts; a user-spec text's entry is written by hand with `push_texts.fingerprint(entry)`.
- The full holdout example sits near the 500-character limit with the Session line counted.

**Ruled out:** applying a reviewer's rewrite of the user's own four lines (objections are recorded, not applied).

### Phase 13 — The remote-control alert in the user's four lines  · status: todo

**Depends on:** Phase 12; shutdown checkpoint 0dcaadac merged to build-followups (it is, as 34e46574)

#### Work Order

**Goal:** The remote-control alert reads Title / Session / Action / Reason in the user's own words.

**Spec:**
- User, 2026-10-14 16:3x EDT (via the showrunner), on the push sent at 16:25 EDT: store this as the template for its type. `shutdown.remote_control` — Title `{who} remote-control is off`; Session `{session}`; Action `run /remote-control`; Reason `Remote control is off.` (the body opens with that `Reason: ` line, as `shutdown.holdout` does). `{who}` is `<unit> unit` for a production unit (`trunk unit`), `<production> showrunner` for a showrunner, and the session name otherwise; `remote_control.remote_control_who` already computes it.
- The one render call, `alert_failed` in `scripts/shutdown/remote_control.py`, becomes `render("shutdown.remote_control", who=..., session=...)` with no `machine`. The session name reaches it through a new `--session` fixer argument: `restart.remote_control_missing` → `launch_fixer` → `fix` → `alert_failed`; `shutdown.py`'s remote-control verb parses the fixer's flags. The catalog entry and its call site land in one commit. Shutdown agreed (2026-10-14 18:29 EDT).
- The entry is the user's spec: run `review.py texts --id shutdown.remote_control` three times, then record its approval with the new flag below; a reviewer objection to the user's words is recorded, never applied.
- Approval of a user-spec text (showrunner, 2026-10-14 19:2x EDT, add-on placed in this phase): `review.py texts --id <id> --approve --user-spec "<source>"` runs the review and writes the `push_texts_reviewed.json` entry even when the verdict is not a pass, with the reviewer string naming the user spec and the entry keeping the reviewer's objections (rule and quote per objection). Without `--user-spec`, `--approve` still writes only passing texts. `--user-spec` without `--approve` is a usage error. Tests: an `ask` verdict with `--user-spec` writes an entry carrying the source and objections and the current fingerprint; without the flag it writes none.

**Files:**
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json`, `scripts/notify/test_push_texts.py` — the entry and its approval
- `scripts/notify/review.py`, `scripts/notify/test_review.py` — `texts --approve --user-spec`
- `scripts/shutdown/remote_control.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/shutdown.py`, `scripts/shutdown/test_remote_control.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_shutdown_cli.py` — the render call and the session name passed to it

**Workers:** 1 writer (no test lane)
- `impl` — everything above except `push_texts_reviewed.json` (the director writes approvals)

**Constraints from prior phases:** No test or check reaches the phone. Run every script suite, the lib suite included. Before dispatch, merge origin/build-followups into this branch: `scripts/shutdown/remote_control.py` exists only after that merge. `restart.remote_control_missing` reaches `alert_failed` two ways — through `launch_fixer` → `fix`, and directly when the fixer cannot launch — and both pass the session name. `test_shutdown_cli.py`'s `test_remote_control_cli_passes_identity_and_failure_alert_policy_to_fixer` calls the remote-control verb without `--session` and asserts `fix()`'s old arguments; update it. Phase 12: `PushText` takes `session=SessionTemplate("{session}")` (`push_texts.py`); the rendered message is `Session: …\nAction: …\n<body>`; `test_push_texts.py`'s length test counts the Session line, so the full example stays under 500 characters. The approval is recorded under the user spec as Phase 12 did: `review.py texts --id shutdown.remote_control` three times; an `ask` on the user's words is recorded, not applied, and the director records the approval with `--approve --user-spec` once it exists (the worker never runs `review.py texts`). Phase 12 wrote its holdout entry by hand with `push_texts.fingerprint(entry)`; that entry's shape is what `--user-spec` writes, plus the objections.

**Acceptance gate:** every script suite green; basedpyright 0/0; the rendered example leads with the four lines exactly; tests where `{who}` differs from the session name (a production unit) show both the fixer's failure alert and the immediate alert after a failed fixer launch carrying `Session: <session name>`.

### Phase 5 — Questions in showrunner replies  · status: todo

**Depends on:** Phase 4, Phase 6, Phase 9, Phase 10

#### Work Order

**Goal:** A question a showrunner asks the user is reviewed when it is recorded, before the reply is written, and the reply must carry the reviewed text.

**Spec:**
- Not before claude 2 refills, 2026-10-15 07:00 EDT (approved design: a failed question costs a showrunner turn on claude 2).
- `scripts/production/update_registration.py` `judgment_state`: an `open_for_user` object item gains an optional `question` field (the full question as it will be asked, multi-line allowed). For each item whose `text` is not in the durable outstanding list (a new question), review its full `question` with `review.review_question` (below; guide tags `question,all`). `ask` → registration fails with `RegistrationFailure(step, "question held for review: …")` listing the questions; the showrunner revises and reruns, and that rerun is the revision (a hold keyed by the question's key, below). `send`/`rewrite` → the reviewed question is stored with the item as `reviewed` and printed as `reviewed question:` lines for the showrunner to paste. `Unreviewed` → stored as written, logged.
- `scripts/hooks/showrunner_footer.py` `block_reason`: for each item in the pending record (`pending_reviewed.json`, below) with a `reviewed` text, block when the reply does not contain that text (whitespace-normalised); block a reply line offering a `(recommended` choice that no recorded item carries. Reason text names the item and says to paste its reviewed question.
- `commands/showrunner/produce.md`: one sentence where `open_for_user` is described (line ~41) — a new question goes in `question`; the reply carries the `reviewed question` text.

- Question holds are separate from push holds: `~/.local/state/notify/questions/<sha256(slug + item text)[:16]>.json`; they never reach the post path and never expire to the phone. A rerun that records the same item is its revision. Test that a held question never calls `post`.
- `scripts/production/dailies_render.py` `read_outstanding()` accepts the optional `question` and `reviewed` fields, and `update_registration.footer()` keeps them when it rebuilds records; tests cover a later update keeping them and reading the full question back.
- Registration writes `pending_reviewed.json` in the showrunner's instance directory (shape under Pending identity); `block_reason` checks the reply against it and deletes it when the reply passes. Later replies carry only the open-question count, as the guide's ask-once rule says.
- The reviewed-text check runs before the footers-off return in `block_reason` and before `stop-showrunner-footer.py`'s `stop_hook_active` skip; test with footers off and on the retry after a block.
- A new question is an `open_for_user` object item whose `text` is not in the outstanding list. Registration refuses a new item without `question` ("a new question needs its full text in `question`"); string items and items already outstanding stay reminders. A question never registered is outside this check; the nightly messages pass counts those.
- Question identity (unit director's call after Phase 3's phase review): a question hold is keyed by `sha256(slug + item text)[:16]`, never by slug alone, so two held questions from one showrunner revise independently; test that case. `judgment_state(path, step)` stays a parser (it is also called for logging, `update_registration.py:334`); the question review runs once, in `update_registration.py footer --state` (the state-writing operation; `register()` schedules jobs and takes no state), after parsing. `review.py` publishes its question-mode API before either worker starts: a `QuestionKey` built from the slug and the item's summary `text`, and `review_question(key: QuestionKey, question: str, revision) -> QuestionOutcome`, guide tags `question,all`, its own `questions/` store, revision found by key, so rewording `question` keeps its revision. It adds a question variant to the existing `PushReview | ReplyContext` review engine rather than a second engine. Tests: a full multi-line question is judged by `question,all` rules with no phone length limits; a reworded question keeps its revision.
- Pending identity (unit director's call after Phase 6's phase review: no turn id exists that registration and the Stop hook share): `pending_reviewed.json` lives in the showrunner's instance directory `showrunner-<slug>/`. Registration finds it by slug; the Stop hook finds it through the instances whose `conf` targets its `session_id` (`showrunner_footer.targeted_instances`). It holds `{items: [...]}` with no session or turn field; each item is tagged: `{kind: "reviewed", text, reviewed}` or `{kind: "review unavailable", text, question, reason}`, converted at the JSON boundary into distinct types. The Stop hook requires the accepted full text for either; history keeps that text; tests round-trip both kinds and confirm an unavailable review is never stored as a completed one. A second registration before a passing reply adds to it; a held question in a batch fails the whole registration and writes nothing (outstanding, pending and history files unchanged); the file is deleted only once the whole Stop-hook check passes, so a retry after a block, a later turn or a replacement session targeting the same instance must still carry the text. The `impl` slot publishes this record shape before the `test` slot writes its reader. Tests use the real Stop-hook payload shape: repeated registration, a partly held batch, an unrelated footer rejection keeping the pending record, a later turn, a replacement session.
- Types: reminders, questions awaiting facts and reviewed questions are separate record types, converted from JSON at the boundary; `Outstanding` gains no further bare optional fields. A question accepted after review and one accepted because review was unavailable (`Unreviewed`) are distinct states, so a reviewed record never claims a review that did not complete. Replace `Outstanding.after: datetime | None` (`dailies_render.py:265`; absence means due now) with explicit due-now and deferred-until states, converting the optional JSON `after` at the boundary in `read_outstanding()`.
- Question content is not a push (architect, after Phase 12): Phase 12 split a leading `Session:` line inside `review.validate()`'s `not reply` branch, and `_review()` tells `PushReview` from `ReplyContext` only. The question context selects question rules and validation itself and never runs push header parsing or push length limits; tests keep a question whose text begins `Session:`, `Action:` or `No action needed.`, and one over 1,024 characters, intact.
- Question records in `review.jsonl` carry a field that marks them as question reviews, and `nightly_review.live_message_counts()` (which counts every record without `replay`, `dry` or `sample` as a live push) skips them; a nightly test shows a question record leaves the push totals unchanged.
- Full-question recall: `update_registration.open_items()` (`update_registration.py:290`; `produce.md` names it for a user asking to see the questions in full) prints each item's stored full `question`, multi-line, and `text` for a reminder; routine footers keep the count only. Test a multi-line recall.

**Files:**
- `scripts/production/update_registration.py` — `question` field, its review, its history line and full-question recall
- `scripts/production/test_update_registration.py` — those cases
- `scripts/hooks/showrunner_footer.py` — reviewed-text and choice-line checks
- `scripts/hooks/test_stop_showrunner_footer.py` — those cases
- `scripts/notify/review.py` — question mode
- `scripts/notify/test_review.py` — question mode
- `commands/showrunner/produce.md` — one sentence near `open_for_user`
- `scripts/production/dailies_render.py` — `read_outstanding()` fields
- `scripts/production/test_dailies_render.py` — those fields
- `scripts/hooks/stop-showrunner-footer.py` — the check runs before the `stop_hook_active` skip
- `scripts/nightly_review/nightly_review.py` — `live_message_counts` leaves question-review records out of push totals
- `scripts/nightly_review/test_nightly_review.py` — that case

**Workers:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `update_registration.py`, `review.py`, `nightly_review.py` and their tests; hub: `scripts/notify/review.py`
- `test` (opens as impl) — `showrunner_footer.py`, `stop-showrunner-footer.py`, `dailies_render.py` and their tests, produce.md sentence; publishes the `Outstanding` due-now/deferred types before `impl` changes `open_items()`

**Constraints from prior phases:** `review.py` API from Phase 2 (`Draft`, `review(draft, revision)`, holds keyed by `source`, `Unreviewed` sends as written); guide tags `question`, `reply`, `all`. Phase 4: one review may take up to `review.review_timeout_seconds()` (`config/notify.conf` `REVIEW_TIMEOUT_SECONDS`, default 60 s); any wait around a review derives from `review.py`'s helpers, never a literal. `test_update_registration.py` copies the command tree into a temp directory without `scripts/notify/`, so import `review` inside the function that reviews, never at module level (a module-level import in `sweep.py` broke that test). Lengths are counted in code and each failure carries `{rule, quote}`. Phase 4: `send.py` reports a superseded push as `REPLACED`, exit 0, like `SKIPPED`. Phase 6: `footer()` appends each newly outstanding item's `text` to `outstanding/<slug>.history.jsonl` (`record_question_history`, kept 48 h), and `review.registered_questions(since)` matches reply question sentences against current outstanding texts plus those history lines for the nightly `review.py sample` count. Keep the history written when `footer()` changes, record the reviewed question (the text the reply carries) beside the item text, and have `registered_questions` read both; tests: a reviewed question counts as registered before and after its item closes, and a held batch writes no history line. `review.py` already holds `ReplyContext`, `PushReview`, `reply_questions` -> `ReplyAsks` (question and request sentences, `(recommended` choice lines; code and `>` quotes excluded) and `sample`; the footer hook's choice-line check reuses that detection, never a second detector. Phase 8: the reviewer binary is resolved by `review.reviewer_binary()` (a timer's PATH may lack `claude`); test fakes match it by basename. Phase 9: `ReviewOutcome` adds `Failed(best_version, failed_rules)` and `Withheld(why)`; a `Rewritten` is re-checked once inside a two-review budget (`delivery_waits()`); the `withhold` verdict is push mode only, so question review never returns it, and question mode keeps an `ask` awaiting facts on both first review and revision, never reply mode's revision-ask conversion to `Rewritten` (`review.py` returns `Questions` only when `not reply or not revision`); a rewrite is accepted only after its re-check approves it, and an `ask`, another rewrite or `Failed` from that re-check fails the whole registration with the review details, keeps the question's revision identity, and leaves outstanding, pending and history files unchanged. Tests: an unresolved `ask` on rerun; a rewrite whose re-check fails. Phase 9 also: every fixed phone text is a `scripts/notify/push_catalog.py` entry sent through `push_texts.user_send_argv` (or `pushover.py --text-id`), and `test_push_texts.py` fails a literal phone send (`--to user`, `--to=user`, a `pushover.py` path, `broadcast.send("user", …)`) anywhere under `scripts/` outside `push_texts.py`, `pushover.py`, `send.py` and `escalate.py`; a question review is not a push and needs no catalog entry. `review.py` also holds the `texts` subcommand and its approval file `push_texts_reviewed.json`; keep `test_push_texts.py` green when `review.py` changes. Dry clarity checks use the worktree's `scripts/notify/review.py`; the installed `~/.claude` copy can lag. Merge origin/build-followups before dispatch: other units' recent work touches `test_update_registration.py`.

**Acceptance gate:** Test lines for `scripts/production`, `scripts/hooks`, `scripts/notify` green; basedpyright 0/0; tests cover a held question failing registration, its rerun accepted, a reply missing the reviewed text blocked, an unrecorded `(recommended` line blocked, a question beginning `Session:` kept intact, a question record left out of the nightly push count; `scripts/nightly_review` tests green.

### Phase 6 — Nightly messages pass  · status: done

#### As-built

- **Nightly `messages` mode** (`scripts/nightly_review/nightly_review.py`, after `rust` in `MODES`): `showrunner_transcripts(since) -> list[Path]` maps each session record under `~/.claude/sessions/` named for a showrunner (`showrunners.py list`) or `natedev` to its transcript in that session's own account home; `live_message_counts` counts the night's live `review.jsonl` verdicts, skipping `replay`, dry `check` and `sample` lines; `messages(since)` runs `review.py sample --limit 20` (timeout `20 * review.user_send_seconds() + 15`) and reports failures by rule, `Unreviewed` apart, and replies holding unregistered questions apart from both. At most one new guide rule goes through `DIR/messages.md` and the ledger to `/nightly_next`.
- **`review.py sample <transcripts...> --limit N --since ISO`** dry-reviews replies that ask nothing (guide tags `reply,all`), each with its date and previous reply (`ReplyContext`), logs `"sample": true`, and posts and holds nothing. `transcript_replies(path, since)` filters by each reply's timestamp, not file modification time. `reply_questions(text) -> ReplyAsks` finds question and request sentences (fenced code, inline code and `>` quotes excluded), apart from `(recommended` choice lines; `registered_questions(since)` reads current outstanding items plus history lines after the cutoff.
- **Question history** (`scripts/production/update_registration.py`): `footer()` appends each newly outstanding item's text to `outstanding/<slug>.history.jsonl`, kept 48 h (`record_question_history`, `QuestionRegistration`), so a question closed earlier in the day does not count as unregistered.
- **Push texts** that pass the reviewer 3 of 3, each text and length pinned by tests: `rust_release.py`'s passed push, `ci_points.py`'s review report (body under 500, keeps the command to read the report), `settle.py`'s holdout (first 150 characters stand alone; whole-sentence grouping; account-wide cancel/now fallback when names do not fit).

**Files:**
- `scripts/nightly_review/nightly_review.py` — `messages` mode, transcript discovery, live counts
- `scripts/notify/review.py` — `sample`, `ReplyContext`, `ReplyAsks`, `reply_questions`, `registered_questions`, `transcript_replies`
- `scripts/production/update_registration.py` — 48 h question history
- `commands/nightly_review.md` — `<Scope mode="messages">`; its one write outside DIR is `review.jsonl` sample lines
- `commands/nightly_next.md` — report description names `messages.md`
- `scripts/buildlog/rust_release.py`, `scripts/production/ci_points.py`, `scripts/shutdown/settle.py` — push texts
- Tests beside each; `scripts/production/test_dailies_input.py` pins the new report text

**Binds later work:** `update_registration.footer()` appends each newly outstanding item's text to `outstanding/<slug>.history.jsonl` (kept 48 h) and `review.registered_questions(since)` reads current outstanding plus that history; any change to `footer()` keeps calling `record_question_history`, and once items carry a reviewed question it records that text too, so the nightly count matches the reply. `review.py` holds `ReplyContext`, `PushReview`, `reply_questions` -> `ReplyAsks` and `sample`, whose unregistered-question count the question-recording work and the nightly-count work build on; question detection elsewhere reuses `reply_questions`.

**Gotchas:**
- Dry clarity checks run the worktree's `scripts/notify/review.py`; the installed `~/.claude` copy can lag with an older schema.
- `nightly_review.py` imports `review` lazily inside `messages()` and `showrunners` only in discovery, because tests copy the library without `scripts/notify/`.
- History stores the item's short `text`, not the full question; a reply matches when either whitespace-normalised string contains the other.
- The unregistered count includes question and request sentences addressed to peers (units, sessions), not only the user.

**Ruled out:** telling questions to the user apart from questions to peers in the nightly count, left to measure once 48 h of history exists.

### Phase 7 — The nightly question count measures only questions put to the user  · status: todo

**Depends on:** Phase 5, Phase 6

#### Work Order

**Goal:** The nightly count of replies holding an unregistered question is re-measured once the question history covers 48 hours, and it stops counting requests to units or sessions if those are still a large part of it.

**Spec:**
- Runs after Phase 5's checkpoint, and not before 48 h after question history began (showrunner's call 2026-10-13: the count is measured against a full history window first). `record_question_history` prunes lines older than 48 h on every write, so the earliest retained line never ages past 48 h and cannot be the clock: at Phase 5's PhaseEnd the unit director reads the earliest `time` in `~/.local/state/showrunner/outstanding/*.history.jsonl` (none existed at 2026-10-13 23:50 EDT) and writes it here as a fixed EDT time; the gate is that time plus 48 h. No history file at that point means the gate stays shut and the read repeats at the next check.
- Measure first, before any code change: a dry pass over the last 24 h of showrunner replies (`nightly_review.showrunner_transcripts(since)`, the worktree's `scripts/notify/review.py`: `transcript_replies`, `reply_questions`, `registered_questions`) that lists, for every reply counted as holding an unregistered question, each unregistered sentence — and for a reply counted only for its `(recommended` choice lines (`sample`'s `asks.has_choices` branch), those lines. Classify each sentence or choice block as put to the user or addressed to a unit or session. Report total counted replies and the share whose unregistered sentences are all addressed to units or sessions. Measurement output goes in `SESSION_DIR`, never in the repository; it posts nothing.
- Build the recipient filter only when that share is one in four or more (unit director's call: "a large part", per the showrunner). Then `reply_questions` stops counting a sentence addressed to a unit or session, by rules derived from the forms the measured peer sentences actually take (for example a sentence inside a message relayed to a session, or one naming a unit or session as its addressee), and keeps every sentence put to the user. Below one in four, the phase ships no code: its checkpoint is the plan doc and the notice.
- The checkpoint notice carries the measured share (counted replies, peer-only replies, percentage) and whether the filter was built (showrunner's request).

**Files:**
- `scripts/notify/review.py` — recipient filter in `reply_questions` (only when built)
- `scripts/notify/test_review.py` — its cases (only when built)

**Workers:** 1 writer
- `impl` — the measurement script (session directory) and, when built, `review.py` and its tests; hub: `scripts/notify/review.py`

**Constraints from prior phases:** Phase 6: `review.py sample` counts a reply as holding an unregistered question when a question or request sentence from `reply_questions` (`ReplyAsks`; fenced code, inline code and `>` quotes excluded) matches no text from `registered_questions(since)` (current outstanding plus 48 h of `outstanding/<slug>.history.jsonl`), and a reply with only `(recommended` choice lines when no registered text appears in it. Phase 5 records the reviewed question text in that history too, and its footer hook reuses `reply_questions` for the choice-line check, so a filter change must keep that check's behavior; run `scripts/hooks` tests as well when the filter is built. Dry checks use the worktree's `review.py`; nothing reaches the phone.

**Acceptance gate:** the measured share is recorded; when the filter is built: test lines for `scripts/notify` and `scripts/hooks` green, basedpyright 0/0, a test with one reply holding a user question and one holding a peer request counts 1, a test with a user-directed and a peer-directed choice-only reply counts 1, and a dry sample over the same 24 h prints its new count beside the measured one.
