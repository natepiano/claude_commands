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

#### Work Order

**Goal:** When the push gate allows a failed revision's fallback, the held version that revision replaced does not reach the phone afterwards (a gate error still allows the fallback, so a rare duplicate stays possible), whether it still sits in `held/` or an expiry worker has claimed it. The reviewer leaves a text that meets the guide unchanged, so a clean script text gets `send` every time.

**Spec:**
- Follow-up from Phase 3 (landing rule, round 3): two fixes left one case open. The gate releases the restored hold through `review.release_if_matches`, which reads only `held/<id>.json`. If `review.py due` has claimed that hold (renamed to `<id>.sending`) and the worker exits before posting, the release finds nothing, the fallback is allowed, and stale-claim recovery posts the held version once more after `CLAIM_STALE_SECONDS`. A live claim posts it at once. Either way the user gets two pushes.
- Release protocol (showrunner's call 2026-10-13, after Phase 3's phase review; losing a push is worse than a rare second copy): `review.py` keeps a delivered record keyed by the hold's identity (`held/delivered/<identity>`, kept 1 hour). `release_if_matches(source, identity)` answers, under the review lock: `delivered` (the record exists), `released` (a matching `.json`, or matching claim older than `CLAIM_STALE_SECONDS`, is deleted and a cancel mark `held/<id>.cancel` holding the identity is written), `posting` (a matching younger claim), or `none` (no hold, or one with another identity, left alone).
- `due()` and a revision's post re-check, under the lock immediately before posting, that their claim still exists and carries no cancel mark for its identity; a cancelled claim is dropped unposted. A successful post writes the delivered record before releasing the lock.
- The gate: `delivered` denies the fallback (`The held version of this push was already sent.`); `released` and `none` allow it; `posting` waits (review plus post plus 5 s, capped at 110 s; the time-limit bullet below), re-asking each second, then follows the delivered or released answer, and allows on timeout. The marker is consumed in every case. Any gate error allows the fallback (a push is never lost), so a rare duplicate remains possible; this plan accepts it.
- `review.py release --identity I -- SOURCE` prints the answer word on one line; the gate's remote path reads it from the ssh output.
- Tests (deterministic, fakes only): expiry delivers then the fallback arrives (local and remote) → denied; posting then worker fails → allowed after release; a suspended worker resuming after release → drops its claim unposted (interleaving test); a newer hold with another identity is kept.
- Reviewer stability (measured 2026-10-13 after Phase 3's repair round 3): re-checking the reviewer's own rewrite gave `send` for 2 of 4 texts; one flipped to `ask`, and one kept failing `push-body-under-500` on a body of about 330 characters. Length limits are counted by `review.validate` in code, never judged by the model; the reviewer prompt (`scripts/notify/reviewer_prompt.md`) says a text that meets every rule is returned unchanged with `send`, and a rule failure must quote the failing words. Test: a fake reviewer that flags `push-body-under-500` on a short body is overruled by the code count.
- Review findings folded after Phase 3 (unit director's calls): length rules are counted in code against both the original and the proposed text (title under 45 characters, action plus body under 500), apart from the 250/1024 transport limits; a false length failure is removed and the other failures kept. A rule failure carries quoted words (`{rule, quote}` in the reviewer schema, `review.py` 196); a quote not found in the text is rejected. The prompt rule that passing text returns unchanged already exists (`reviewer_prompt.md` 3): this phase enforces it. `quota_alert.UserPush.ask: Ask | None` becomes two delivery roles (a phone push with its ask, a log-only note). The checkpoint notice hands the showrunner a fake-payload check that the installed settings hook denies a push with no marker.
- Live gate: the 15 Phase 3 script texts (`round3_samples.txt` shape, rendered from the code) each get at most three live checks; a quoted failure is fixed in the script and that script re-checked; a text still not `send` after three is listed in the notice. Deterministic tests, not the live gate, prove the code rules.
- The reviewer's time limit (the user, 2026-10-13: "20 seconds is too short a time for a reviewer - it needs to be configurable and longer"): `review.py`'s fixed `TIMEOUT_S = 20` becomes `REVIEW_TIMEOUT_SECONDS` in a new committed `config/notify.conf` (one `KEY=VALUE` line with a comment; never `config/agents.conf`, which is pinned), default 60 (live reviews measured 10–13 s; 60 is over four times the slowest). `review.py` reads it once per call, and a missing file, missing key or bad value falls back to 60 and logs why; every other wait derives from it in one place in `review.py`, never a second constant: a revision's budget is review plus the 15 s post; `send.py`'s user-delivery wait, and the 60 s user-send waits in `quota_alert.py` and `rust_release.py`, become review plus post plus a 10 s margin (85 s at the default); `CLAIM_STALE_SECONDS` stays above review plus post (at least 120); the gate's `posting` wait becomes review plus post plus 5 s, capped at 110 s so the hook ends inside its settings timeout of 120 s (the showrunner adds `"timeout": 120` to the PushNotification entry; Claude Code's default is 60 s). Tests: the config parse and its fallback, and each derived wait at a non-default value.

**Files:**
- `scripts/notify/review.py` — `release_if_matches` outcomes and the `release` command's output
- `scripts/notify/test_review.py` — those cases
- `scripts/hooks/pre-tool-use-push-gate.py` — deny on `posting`, read the remote outcome
- `scripts/hooks/test_push_gate.py` — those cases
- `scripts/notify/reviewer_prompt.md` — unchanged text returns `send`; failures quote their words
- `scripts/whoami/quota_alert.py`, `scripts/whoami/test_quota_alert.py` — the two delivery roles
- `scripts/buildlog/rust_release.py` — its push text, only where a quoted rule fails
- `scripts/buildlog/test_rust_release.py` — its push text, only where a quoted rule fails
- `scripts/lint/sweep.py` — its push text, only where a quoted rule fails
- `scripts/lint/test_sweep.py` — its push text, only where a quoted rule fails
- `scripts/production/ci_points.py` — its push text, only where a quoted rule fails
- `scripts/production/test_ci_points.py` — its push text, only where a quoted rule fails
- `scripts/production/codex_winddown.py` — its push text, only where a quoted rule fails
- `scripts/production/test_codex_winddown.py` — its push text, only where a quoted rule fails
- `scripts/shutdown/restart.py` — its push text, only where a quoted rule fails
- `scripts/shutdown/test_restart.py` — its push text, only where a quoted rule fails
- `scripts/shutdown/settle.py` — its push text, only where a quoted rule fails
- `scripts/shutdown/test_settle.py` — its push text, only where a quoted rule fails
- `scripts/production/test_dailies_input.py` — its ci_points send assertions
- `scripts/notify/pushover.py` — a claimed revision posts through `review.post_claim`
- `scripts/notify/test_pushover.py` — that path and a revision cancellation interleaving test
- `config/notify.conf` — new: `REVIEW_TIMEOUT_SECONDS=60`
- `scripts/message/send.py` — its user-delivery wait derives from the reviewer limit
- `scripts/message/test_send.py` — that wait

**Seats:** 2 writers
- `impl` — `review.py`, `reviewer_prompt.md`, the push gate, `pushover.py`'s revision post, `config/notify.conf`, `send.py`'s wait and their tests; hub: `scripts/notify/review.py`
- `test` — `quota_alert.py` (delivery roles and its send wait), `rust_release.py`'s send wait, the script texts and their tests

**Constraints from prior phases:** Phase 3 built the gate: the marker `failed/<session_id>.json` holds `fallback`, the restored hold's `source`, its identity (`review.hold_identity`) and the delivery machine; the gate allows when the push's `message` matches `fallback`, consumes the marker, then releases locally or over `ssh -o BatchMode=yes <machine> … review.py release --identity <id> -- <source>`; any gate error allows. Fix 1 released by source and deleted newer holds; fix 2 added the identity check but reads only `.json`. A claim is a stamped rename to `<id>.sending`; `due` takes over a claim older than 120 s.

**Acceptance gate:** Test lines for `scripts/notify`, `scripts/hooks`, `scripts/message`, `scripts/whoami`, `scripts/buildlog`, `scripts/lint`, `scripts/production`, `scripts/shutdown`, run one after another, green; basedpyright 0/0 on the changed files; the tests above present; the live gate above.

### Phase 5 — Questions in showrunner replies  · status: todo

#### Work Order

**Goal:** A question a showrunner asks the user is reviewed when it is recorded, before the reply is written, and the reply must carry the reviewed text.

**Spec:**
- Not before claude 2 refills, 2026-10-15 07:00 EDT (approved design: a failed question costs a showrunner turn on claude 2).
- `scripts/production/update_registration.py` `judgment_state`: an `open_for_user` object item gains an optional `question` field (the full question as it will be asked, multi-line allowed). For each item whose `text` is not in the durable outstanding list (a new question), call `review.review` in question mode (`Draft` with `source` the showrunner slug, priority 0, no title, the question as body, `answerable` true; guide tags `question,all`). `ask` → registration fails with `RegistrationFailure(step, "question held for review: …")` listing the questions; the showrunner revises and reruns, and that rerun is the revision (a hold keyed by the slug, as in Phase 2). `send`/`rewrite` → the reviewed question is stored with the item as `reviewed` and printed as `reviewed question:` lines for the showrunner to paste. `Unreviewed` → stored as written, logged.
- `scripts/hooks/showrunner_footer.py` `block_reason`: for each item first recorded this turn with a `reviewed` text, block when the reply does not contain that text (whitespace-normalised); block a reply line offering a `(recommended` choice that no recorded item carries. Reason text names the item and says to paste its reviewed question.
- `commands/showrunner/produce.md`: one sentence where `open_for_user` is described (line ~41) — a new question goes in `question`; the reply carries the `reviewed question` text.

- Question holds are separate from push holds: `~/.local/state/notify/questions/<sha256(slug + item text)[:16]>.json`; they never reach the post path and never expire to the phone. A rerun that records the same item is its revision. Test that a held question never calls `post`.
- `scripts/production/dailies_render.py` `read_outstanding()` accepts the optional `question` and `reviewed` fields, and `update_registration.footer()` keeps them when it rebuilds records; tests cover a later update keeping them and reading the full question back.
- Turn identity: registration writes `pending_reviewed.json` in the showrunner's instance directory (`[{text, reviewed}]`); `block_reason` checks the reply against it and deletes it when the reply passes. Later replies carry only the open-question count, as the guide's ask-once rule says.
- The reviewed-text check runs before the footers-off return in `block_reason` and before `stop-showrunner-footer.py`'s `stop_hook_active` skip; test with footers off and on the retry after a block.
- A new question is an `open_for_user` object item whose `text` is not in the outstanding list. Registration refuses a new item without `question` ("a new question needs its full text in `question`"); string items and items already outstanding stay reminders. A question never registered is outside this check; the nightly messages pass counts those.
- Question identity (unit director's call after Phase 3's phase review): a question hold is keyed by `sha256(slug + item text)[:16]`, never by slug alone, so two held questions from one showrunner revise independently; test that case. `judgment_state(path, step)` stays a parser (it is also called for logging, `update_registration.py:334`); the question review runs once, in registration, after parsing. `review.py` publishes its question-mode API before either seat starts: `review_question(slug, text, revision) -> QuestionOutcome`, guide tags `question,all`, its own `questions/` store, revision found by the same key.
- Turn identity: `pending_reviewed.json` holds `{session_id, turn, items: [{text, reviewed}]}`; a second registration in the same turn adds to it, a held question in a batch fails the whole registration and writes nothing, and the file is deleted only once the whole Stop-hook check passes. Tests: repeated registration, a partly held batch, an unrelated footer rejection, a later turn.
- Types: reminders, questions awaiting facts and reviewed questions are separate record types, converted from JSON at the boundary; `Outstanding` gains no further bare optional fields.

**Files:**
- `scripts/production/update_registration.py` — `question` field and its review
- `scripts/production/test_update_registration.py` — those cases
- `scripts/hooks/showrunner_footer.py` — reviewed-text and choice-line checks
- `scripts/hooks/test_stop_showrunner_footer.py` — those cases
- `scripts/notify/review.py` — question mode
- `scripts/notify/test_review.py` — question mode
- `commands/showrunner/produce.md` — one sentence near `open_for_user`
- `scripts/production/dailies_render.py` — `read_outstanding()` fields
- `scripts/production/test_dailies_render.py` — those fields
- `scripts/hooks/stop-showrunner-footer.py` — the check runs before the `stop_hook_active` skip

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `update_registration.py`, `review.py` and their tests; hub: `scripts/notify/review.py`
- `test` (opens as impl) — `showrunner_footer.py`, `stop-showrunner-footer.py`, `dailies_render.py` and their tests, produce.md sentence

**Constraints from prior phases:** `review.py` API from Phase 2 (`Draft`, `review(draft, revision)`, holds keyed by `source`, `Unreviewed` sends as written); guide tags `question`, `reply`, `all`.

**Acceptance gate:** Test lines for `scripts/production`, `scripts/hooks`, `scripts/notify` green; basedpyright 0/0; tests cover a held question failing registration, its rerun accepted, a reply missing the reviewed text blocked, an unrecorded `(recommended` line blocked.

### Phase 6 — Nightly messages pass  · status: todo

#### Work Order

**Goal:** Each night the review reads the day's verdicts and a sample of showrunner replies, queues at most one new guide rule for the user's yes or no, and counts how many replies that ask nothing would have failed.

**Spec:**
- `scripts/nightly_review/nightly_review.py` `MODES` gains `messages` (after `rust`); `commands/nightly_review.md` gains `<Scope mode="messages">`: read the night's `~/.local/state/notify/review.jsonl` lines and the last 24 h of showrunner replies (the transcripts of sessions `showrunners.py` lists, under the unit account's `projects/` directory); run `review.py replay`-style dry reviews over up to 20 replies that ask nothing (reply guide tags `reply,all`) and report how many fail and which rules; propose at most one new rule (stem, test, source quoting the user, a Bad/Good pair from tonight's messages) through the usual `DIR/messages.md` and ledger path, for `/nightly_next`.
- `review.py` gains `review.py sample <transcript-paths...> --limit N` for that dry review (posts nothing, holds nothing, logs `"sample": true`).

- Transcript discovery (in `nightly_review.py`, published to the `review.py sample` writer as `showrunner_transcripts(since) -> list[Path]`): every session record under `~/.claude/sessions/` whose name is a showrunner's (`showrunners.py list`) or the standing session `natedev`, mapped to `<unit account config dir>/projects/<cwd slug>/<session id>.jsonl`, modified in the last 24 h.
- Counts: replies only; `replay`, `check` and `sample` lines never count as live messages; `Unreviewed` is reported apart from failures; each sampled reply goes to the reviewer with its date and the previous reply, so ask-once and times-carry-zone judge it in context.
- `commands/nightly_review.md`: the argument hint and proposal Target template gain `messages` and the guide path; "write nothing outside DIR" names its one exception, `review.jsonl` sample lines. `commands/nightly_next.md`'s report-file description names `messages.md`.

**Files:**
- `scripts/nightly_review/nightly_review.py` — `messages` mode
- `scripts/nightly_review/test_nightly_review.py` — that mode
- `commands/nightly_review.md` — `<Scope mode="messages">`
- `scripts/notify/review.py` — `sample`
- `scripts/notify/test_review.py` — `sample`
- `commands/nightly_next.md` — `messages.md` in the report description

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `review.py sample` and its tests; hub: `scripts/notify/review.py`
- `test` (opens as impl) — `nightly_review.py`, its test, `commands/nightly_review.md`, `commands/nightly_next.md`

**Constraints from prior phases:** `review.jsonl` line shape from Phase 2; reply review uses guide tags `reply,all`; the question review of Phase 5 already covers replies that ask. Unregistered questions (unit director's call after Phase 3's phase review): the nightly pass also counts replies that ask the user a question no registration record carries, reported apart from failures and from `Unreviewed`; test a transcript holding one. Transcript discovery uses each showrunner session's own account (showrunners run on the showrunner account, `scripts/production/add_showrunner.py:120`) and its recorded transcript path, and filters by each reply's timestamp, not file modification time; test distinct account homes, a renamed session and an old transcript modified today.

**Acceptance gate:** Test lines for `scripts/nightly_review` and `scripts/notify` green; basedpyright 0/0; `review.py sample` over two real transcripts prints verdicts and posts nothing.
