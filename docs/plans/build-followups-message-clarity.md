# Message clarity: every push and every question reaches the user reviewed

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A written message guide, a reviewer that reads every phone push cold before it is sent (send, rewrite, or hold with questions for the writer), a gate on Claude's built-in push, the same review for questions a showrunner asks in a reply, and a nightly pass that grows the guide.

> **As-built disposition: create** — a new `docs/as-built/message-clarity.md`

> **Production: build-followups** — unit `message-clarity-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-13 07:32 EDT

I want you to permanently address the incomprehensibility of this message. When you use the message infra to send a message to the user there needs to be an agent in the mix that reviews such messages for clarity as this one is not at all. That agent needs to act as my advocate for clear communication. Maybe it's a permanent showrunner you relay messages through. Maybe a transient headless session. Maybe it builds a set of style guidelines like /rust_style but for messaging improvement.

The design is `~/.local/state/plan-backlog/2026-10-13-user-message-clarity.md`, option (d), approved by the user 07:4x EDT 2026-10-13 (showrunner's word). Its "Calls I made" stand. The message in question is the 02:19 EDT push "Hana: nested-g2 wrap".

## Delegation Context

- **Project:** `~/.claude` config repo, plus the style repo `~/rust/nate_style`. Worktree `/home/natepiano/worktrees/claude-build-followups-message-clarity`, branch `build-followups-message-clarity` (unit `message-clarity-unit`); name both in every dispatch. Guide files go in the nate_style worktree `/home/natepiano/worktrees/nate_style-message-clarity`, branch `message-clarity` (the showrunner merges it to nate_style main with the phase; showrunner's call 2026-10-13).
- **Project started:** <!-- written by progress_history.py start-run -->
- **Stack:** Python 3 standard library, checked by basedpyright; zsh; Markdown commands and rule files; `claude -p` for the reviewer.
- **Layout:** `scripts/notify/` (owned: `pushover.py`, `user_action.py`, new `review.py`, `reviewer_prompt.md`, tests) · `scripts/message/send.py` and `commands/message.md` (owner shutdown-unit; also touches) · `commands/alert_user.md`, `commands/showrunner/produce.md` `<Notify>` (hub, owner stalls-unit; also touches) · `docs/user_facing_explanation.md` · new `commands/message_style.md`, `commands/message_review.md` · new `scripts/hooks/pre-tool-use-push-gate.py` · `scripts/production/update_registration.py`, `scripts/hooks/showrunner_footer.py` (Phase 4) · `scripts/nightly_review/nightly_review.py`, `commands/nightly_review.md` (Phase 5) · `~/rust/nate_style/message/` (new). State: `~/.local/state/notify/` (`pushover.jsonl` today; new `review.jsonl`, `held/`, `failed/`, `review_off`).
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
- **Test:** `python3 -m unittest discover -s scripts/notify -p 'test_*.py'` and `python3 -m unittest discover -s scripts/message -p 'test_*.py'`; Phase 4 adds `-s scripts/production` and `-s scripts/hooks`; Phase 5 adds `-s scripts/nightly_review`.
- **Lint:** `basedpyright <changed .py files>` — pass is its `0 errors, 0 warnings` line (it exits 3 on the missing `.venv`).
- **Invariants:**
  - **No test or live check reaches the phone** (user rule, memory `phone-push-must-be-actionable`; showrunner 2026-10-13). Tests fake the reviewer process and `post`; a live check uses `review.py replay` or `--dry-run`, which never posts.
  - **A push is never lost.** Reviewer missing, slow (over 30 s), failing, out of quota, or returning unreadable JSON → the push goes out as written and the log marks it `unreviewed: <why>` (approved design).
  - **The reviewer never adds a fact.** It may cut, reorder and reword; whatever is missing it asks for (approved design).
  - **No skip option.** `pushover.py` has no flag that bypasses review; only the user's switch (`review_off`) turns it off (approved design).
  - **The reviewer keeps the action form and priority.** An `--action` push stays an action push (non-empty action text); a `--no-action` push stays one; priority never changes.
  - Pushover limits: title ≤ 250, message ≤ 1024 including the first line.
  - Do not commit `settings.json` or `config/agents.conf`: the showrunner applies the exact proposed lines from the phase notice (showrunner's call 2026-10-13).
  - Python: basedpyright 0/0 on changed files; no file-level ignores; `TypedDict` for every JSON shape; no `Any`.

## Phases

### Phase 1 — The message guide and /message_style  · status: todo

#### Work Order

**Goal:** `/message_style` loads a written guide of the user's message rules, and every command that writes to the user loads it before writing.

**Spec:**
- Create `~/rust/nate_style/message/` in the nate_style worktree (Delegation Context). One rule per file, in the ux rule-file shape (Key files), with `tags` from `push`, `question`, `reply`, `all`, and `source` quoting the user's words with their date from the source memory or doc. Each `test` is one question a cold reader can answer yes/no from the message alone. Rules (stem — what it says):
  - `reader-starts-cold` — The user knows only what the message says. Every name (session, unit, phase, script, flag, commit, file) is either said in plain words by what it does, or dropped. Source: `docs/user_facing_explanation.md` "What the user knows" and its banned vocabulary.
  - `what-happened-why-what-you-do` — In order: what happened, why it matters to the user, what they do and where. Source: the approved design; `answer-in-a-few-lines`.
  - `every-choice-says-what-it-does` — Each option names what happens if chosen; exactly one is recommended, with its reason. Source: `docs/user_facing_explanation.md` (one recommendation).
  - `say-where-to-act` — An action names the machine and the session or terminal the user acts in. Source: produce.md `<Notify>` ("the exact action, and on which machine").
  - `title-names-the-stake` — A push title is under 45 characters and names what is at stake; no fixed prefix (the "Hana:" prefix on a push about nothing in Hana is the counterexample). Source: the approved design.
  - `first-150-stand-alone` — The first 150 characters of a push make sense alone (the phone preview). Source: the approved design.
  - `push-body-under-500` — A push body is under 500 characters. Source: the approved design.
  - `times-carry-zone` — Every time names its zone (EDT) and its date when not today. Source: `name-the-time-zone`.
  - `push-only-what-the-user-must-do` — A push names something only the user can do, or a `note` case produce.md lists; anything for an agent goes to it by message. Source: `phone-push-must-be-actionable`, `hooks-speak-only-when-actionable`.
  - `short-plain-sentences` — Short sentences, plain words, no restatement, no flourish. Source: `speak-plainly`, `speak-simply-and-briefly`.
  - `before-and-after` — A change is told as what the user sees before and after it. Source: `docs/user_facing_explanation.md` "behavior before name".
  - `ask-once-then-count` (tags `question`, `reply`) — A question is asked once in full; later replies say only "There are N open questions for you." Source: `open-questions-as-a-count`.
  - `explain-means-explain` (tags `reply`) — Asked to explain, rebuild the picture from what the user knows; never repeat the jargon louder. Source: `explain-means-explain`.
- Each rule's `### Bad` / `### Good` pair comes from a real message where one exists; `reader-starts-cold`, `what-happened-why-what-you-do`, `every-choice-says-what-it-does` and `title-names-the-stake` use the 02:19 EDT push and the reviewer's version quoted in the design ("Tonight's push, through the reviewer").
- `~/rust/nate_style/message/README.md` (no `test:`, so the loader skips it): one paragraph — what the guide is for, who loads it, and that each rule names its source. `~/rust/nate_style/README.md` Structure: add a `message/` line.
- New `commands/message_style.md` (frontmatter `description: Load the message guide before writing anything the user reads — a push, a question, a report.`): run `zsh ~/.claude/scripts/ux_style/load-ux-style.sh --guide ~/rust/nate_style/message`; read the persisted output whole if the Bash tool saved it; confirm the rule count. Follow `/succinct_style`.
- Loaders: `commands/alert_user.md`, `commands/message.md` (its "Message the user" paragraph) and `commands/showrunner/produce.md` `<Notify>` each gain one line: load `/message_style` before the first message to the user in a session. `docs/user_facing_explanation.md`: one line pointing to the guide for wording; its decision method stays.
- produce.md `<Notify>`: the send line's `--summary "Hana: <unit or topic>"` becomes `--summary "<what is at stake, under 45 characters>"`; "The message is the one action or fact, under 200 characters" becomes "under 500 characters, per the message guide".
- Memories stay as they are: they are the sources each rule quotes (unit director's call; a memory edit is not this unit's scope).

**Files:**
- `~/rust/nate_style/message/*.md` (new, 13 rules + README), `~/rust/nate_style/README.md` — in the nate_style worktree
- `commands/message_style.md` (new)
- `commands/alert_user.md`, `commands/message.md`, `commands/showrunner/produce.md` (`<Notify>` only), `docs/user_facing_explanation.md` — one line each, plus the produce.md summary/limit edits

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — the 13 rule files and both READMEs in the nate_style worktree
- `test` (opens as impl) — `commands/message_style.md` and the four loader edits; hub: none

**Constraints from prior phases:** none.

**Acceptance gate:** `zsh scripts/ux_style/load-ux-style.sh --guide /home/natepiano/worktrees/nate_style-message-clarity/message --checklist` prints 13 lines, one per rule, each with a test; the full load prints every body; `grep -rn "Hana: <unit" commands/showrunner/produce.md` finds nothing; each rule's `source` quotes words that exist in its named source.

### Phase 2 — The push reviewer  · status: todo

#### Work Order

**Goal:** Every phone push is read cold by a reviewer before it is sent: sent as written, sent rewritten, or held with up to three questions for the session that wrote it; the writer sees exactly what was sent.

**Spec:**
- New `scripts/notify/review.py`. Public surface used by `pushover.py`: `review(draft: Draft, revision: bool) -> Verdict | Unreviewed`, `held_for(source: str) -> Held | None`, `hold(...)`, `release(source)`, `log_review(...)`; commands `review.py due`, `review.py replay [--limit N]`, `review.py switch on|off|status`.
  - `Draft` (frozen dataclass): `source`, `priority`, `title`, `action: UserAction`, `body` (the body without the first line), `answerable: bool`.
  - Reviewer process: `claude -p --model <agent> --effort <effort> --system-prompt <guide + reviewer prompt> --json-schema <schema> --output-format json --tools "" --no-session-persistence --setting-sources ""` (confirm each flag with `claude --help` and drop one that does not exist, saying so), the draft as the prompt (title, action line or "no action", body, priority, and whether it is a revision), environment from `accounts.role_account("unit")` → `named_account_environment(dir)` for a `NamedAccount`, unchanged for `DefaultAccount`; model and effort from `agents_resolve_print message_review.reviewer` (`bash -c 'source ~/.claude/scripts/agents/agents_config.sh; agents_resolve_print message_review.reviewer'`), falling back to `opus` / `medium` when unresolved. Timeout 30 s. The process runner is injectable for tests.
  - Guide text: the full output of `load-ux-style.sh --guide ~/rust/nate_style/message --tags push,all` (tag filter: any match), read once per call; missing guide → `Unreviewed("no guide")`.
  - `scripts/notify/reviewer_prompt.md` (new): the reviewer is the user's advocate for clear messages; it has seen none of the work; it judges the draft against the guide; it never adds a fact, and asks for what is missing; at most three questions, each one the user would ask; it keeps the action form (action stays an action, no-action stays no-action) and never changes what the user is asked to do; on a revision it may not ask.
  - Verdict JSON schema: `{"verdict": "send" | "rewrite" | "ask", "title": str, "action": str | null, "body": str, "questions": [str] (≤3, empty unless ask), "failed_rules": [str]}`. `title`/`action`/`body` always hold its best version (for `send`, the draft unchanged). Validation in `review.py`: verdict in set; on revision `ask` becomes `rewrite`; title ≤ 250; action non-empty iff the draft had one; posted message ≤ 1024; questions ≤ 3. A failed check → `Unreviewed("<which>")`.
- Outcomes in `pushover.send()`, after the action checks and before the keys are read:
  - Switch off (`~/.local/state/notify/review_off` exists) → send as written, log `review: off`.
  - `Unreviewed(why)` → send as written, log `unreviewed: <why>`.
  - `send` / `rewrite` → post the reviewer's version; stdout `pushover: sent` plus, for a rewrite, `pushover: sent as rewritten:` and the sent title, action line and body on the following lines.
  - `ask` with `answerable` → no post; write the hold `~/.local/state/notify/held/<sha256(source)[:16]>.json` (`source`, `priority`, `created` epoch, draft, best version, questions); register notifier job `notify-review` as `escalate.schedule()` does (`--every 1 --run "<py> <review.py> due"`); exit 4, stderr `pushover: held for review: answer these in a revised push within 5 minutes; otherwise the reviewer's version is sent:` then one numbered question per line.
  - `ask` without `answerable` (a script's fixed text) → post the best version at once; log `rewritten, script text failed: <failed_rules>`.
  - A push from a source with a live hold is its revision: `review(..., revision=True)`, then post (send or rewrite), delete the hold.
- `review.py due`: each hold older than 300 s → post its best version through `pushover`'s post path (same keys, log line `sent after hold expired`), delete the hold. Holds are written and removed under one `fcntl` lock file, as `escalate.locked()` does.
- Log `~/.local/state/notify/review.jsonl`, one line per review: `time` (ISO with offset), `source`, `priority`, `draft` (title, action, body), `verdict`, `failed_rules`, `questions`, `sent` (title, action, body) or null, `seconds`, `account` label, `model`. `pushover.jsonl` keeps its existing line with the posted text.
- `review.py replay [--limit N]`: reads `pushover.jsonl` lines whose outcome starts with `sent`, newest first (default all), reviews each as a non-revision, non-answerable draft, prints per push: time, verdict, failed rules, and the reviewer's version; posts nothing and writes no hold; logs with `"replay": true`.
- `review.py switch on|off|status` creates/removes `review_off` and prints the state; new `commands/message_review.md` (`description: Turn the review of pushes to the user's phone on or off, or show its state.`) runs it, mirroring `commands/banned_word_hook.md` (empty argument → ask on/off and stop).
- `send.py`:
  - `answerable` is true when the send has no `--from` and `default_sender()` resolved a session name; `user()` passes `--answerable` to pushover then. `remote()` forwards `--answerable` for a `--to user` send that is answerable here, and `send.py` accepts it as an internal flag (`--answerable`, only with `--to user`).
  - Pushover exit 4 → new outcome `held` (printed `HELD: <the questions, all lines, untruncated>`, exit 4); `remote()` recognises `HELD`. Exit 0 with `sent as rewritten:` → `SENT: to the user, rewritten:` followed by the sent text lines (not passed through `one_line`).
  - `pushover.py` `parse` accepts `--answerable` (no value).
- `commands/message.md` "Outcomes and recovery": add `HELD: <questions>` (exit 4): the reviewer held your push; send a revision with the answers within 5 minutes (the same `--summary` is not required); a revision is never held again; with no revision the reviewer's version goes out at 5 minutes. And: `SENT: to the user, rewritten:` shows exactly what the user received.
- Proposed lines for the showrunner (not committed): `config/agents.conf` `[assignments]` gains `message_review=claude`, and a new section `[message_review.claude]` with `reviewer=opus:medium` (unit director's call: medium keeps a review near 10 s; `/agent` can raise it).

**Files:**
- `scripts/notify/review.py`, `scripts/notify/reviewer_prompt.md`, `scripts/notify/test_review.py` (new)
- `scripts/notify/pushover.py`, `scripts/notify/test_pushover.py`
- `scripts/message/send.py`, `scripts/message/test_send.py`
- `commands/message.md` (outcomes), `commands/message_review.md` (new)

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `scripts/notify/review.py`, `reviewer_prompt.md`, `pushover.py`, `commands/message_review.md`; hub: `scripts/notify/pushover.py` (the exit-4 and stdout contract `send.py` parses)
- `test` (opens as impl) — `scripts/notify/test_review.py`, `test_pushover.py` (fake process runner, fake `post`, temp state dir via env or patched paths: send, rewrite, ask+answerable holds and exits 4, ask+script posts at once, revision never held, timeout/bad JSON/non-zero exit → sent as written and logged unreviewed, switch off, due after 300 s, action-form checks), and `scripts/message/send.py`, `test_send.py`, `commands/message.md`

**Constraints from prior phases:** `scripts/message/send.py` and `test_send.py` edits land only after shutdown-unit's Phases 8–9 merge to `build-followups` (showrunner, 2026-10-13); merge `build-followups` into this branch first, or tell the showrunner the phase waits on them. The guide is `~/rust/nate_style/message/` (on main once the showrunner merges the Phase 1 nate_style branch; until then read it from the nate_style worktree), loaded by `scripts/ux_style/load-ux-style.sh --guide <dir>`; rule tags are `push`, `question`, `reply`, `all`.

**Acceptance gate:** both Test lines green; basedpyright 0/0 on changed files; `review.py replay --limit 5` (unit director, live: reviewer runs, nothing posts) prints five verdicts, and its rewrite of the 2026-10-13 02:19 EDT push passes the guide's checklist by the director's reading; `grep -c '"sent"' ~/.local/state/notify/pushover.jsonl` unchanged across the run.

### Phase 3 — The built-in push gate and every script's fixed push text  · status: todo

#### Work Order

**Goal:** Claude's built-in PushNotification works only right after a failed send and carries the reviewed text; every script's fixed push text passes the guide.

**Spec:**
- On `FAILED` for a `--to user` send (pushover exit 1 after review), `send.py` writes `~/.local/state/notify/failed/<session_id>.json` (`time`, the reviewed title/action/body that failed to post) when it runs under a Claude session (session id from the `~/.claude/sessions/$CLAUDE_PID.json` record), and its `FAILED:` output ends with `fallback text:` and the reviewed text, so the fallback carries it. `pushover.py` prints the reviewed text on its failure path for `send.py` to pass on.
- New `scripts/hooks/pre-tool-use-push-gate.py` (PreToolUse, matcher `PushNotification`): reads the hook payload's `session_id`; allows when `failed/<session_id>.json` is under 10 minutes old and deletes it; otherwise denies with: `PushNotification is only the fallback for a failed send. Send with ~/.claude/scripts/message/send.py --to user (see /alert_user); it is reviewed for clarity first.` Any hook error allows (a push is never lost) and prints the error to stderr. Test file `scripts/hooks/test_push_gate.py`.
- Proposed `settings.json` lines for the showrunner (not committed): a `PreToolUse` entry `{"matcher": "PushNotification", "hooks": [{"type": "command", "command": "~/.claude/scripts/lib/py ~/.claude/scripts/hooks/pre-tool-use-push-gate.py"}]}` (copy the exact command form the existing `mcp__brp__brp_launch` entry uses).
- `commands/alert_user.md` and produce.md `<Notify>`: the `FAILED` fallback line becomes "fall back to PushNotification with the `fallback text:` that `send.py` printed".
- Fixed push texts: read each script's user-bound text once against the guide's `push` and `all` checklist, and fix every one that fails where it is written: `scripts/buildlog/rust_release.py`, `scripts/lint/sweep.py`, `scripts/production/ci_points.py`, `scripts/production/codex_winddown.py`, `scripts/shutdown/{record,restart,settle,shutdown}.py`, `scripts/whoami/quota_alert.py`, `scripts/message/escalate.py` (only the text it composes). Each sends with `--from <script name>` (add it where missing, so the reviewer never holds a script). Their tests that assert the old text change with it.
- Unit director's note in the phase notice: each script file's owner unit, so the showrunner can tell them.

**Files:**
- `scripts/message/send.py`, `test_send.py`; `scripts/notify/pushover.py`, `test_pushover.py`
- `scripts/hooks/pre-tool-use-push-gate.py`, `scripts/hooks/test_push_gate.py` (new)
- `commands/alert_user.md`, `commands/showrunner/produce.md` (`<Notify>` fallback line)
- the ten script files above and their `test_*.py` where text is asserted

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `send.py`, `pushover.py`, the push gate hook and their tests, the two command edits; hub: `scripts/message/send.py`
- `test` (opens as impl) — the ten scripts' fixed texts and their tests

**Constraints from prior phases:** Before dispatch, check each of the ten scripts against its owner's branch and worktree for unmerged edits; `scripts/shutdown/*` and `scripts/message/send.py` land only after shutdown-unit's Phases 8–9 merge (showrunner, 2026-10-13), else tell the showrunner which file blocks. Phase 2's contract: pushover exit 0 sent (stdout `sent as rewritten:` + lines on a rewrite), 1 post failed, 2 refused, 4 held; `send.py` outcomes add `HELD` (exit 4); scripts with `--from` are never held (their failed text is rewritten at once and logged `rewritten, script text failed`); `review.py replay` reviews without posting.

**Acceptance gate:** Test lines for `scripts/notify`, `scripts/message`, `scripts/hooks` green; basedpyright 0/0; the hook test covers allow-after-failure, deny otherwise, marker consumed, hook error allows; `review.py replay` over each changed script's new text (fed as drafts by the director) returns `send`.

### Phase 4 — Questions in showrunner replies  · status: todo

#### Work Order

**Goal:** A question a showrunner asks the user is reviewed when it is recorded, before the reply is written, and the reply must carry the reviewed text.

**Spec:**
- Not before claude 2 refills, 2026-10-15 07:00 EDT (approved design: a failed question costs a showrunner turn on claude 2).
- `scripts/production/update_registration.py` `judgment_state`: an `open_for_user` object item gains an optional `question` field (the full question as it will be asked, multi-line allowed). For each item whose `text` is not in the durable outstanding list (a new question), call `review.review` in question mode (`Draft` with `source` the showrunner slug, priority 0, no title, the question as body, `answerable` true; guide tags `question,all`). `ask` → registration fails with `RegistrationFailure(step, "question held for review: …")` listing the questions; the showrunner revises and reruns, and that rerun is the revision (a hold keyed by the slug, as in Phase 2). `send`/`rewrite` → the reviewed question is stored with the item as `reviewed` and printed as `reviewed question:` lines for the showrunner to paste. `Unreviewed` → stored as written, logged.
- `scripts/hooks/showrunner_footer.py` `block_reason`: for each item first recorded this turn with a `reviewed` text, block when the reply does not contain that text (whitespace-normalised); block a reply line offering a `(recommended` choice that no recorded item carries. Reason text names the item and says to paste its reviewed question.
- `commands/showrunner/produce.md`: one sentence where `open_for_user` is described (line ~41) — a new question goes in `question`; the reply carries the `reviewed question` text.

**Files:**
- `scripts/production/update_registration.py` and its test
- `scripts/hooks/showrunner_footer.py`, its test
- `scripts/notify/review.py` (question mode: no title, guide tags), `test_review.py`
- `commands/showrunner/produce.md` (one sentence)

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `update_registration.py`, `review.py` and their tests; hub: `scripts/notify/review.py`
- `test` (opens as impl) — `showrunner_footer.py` and its test, produce.md sentence

**Constraints from prior phases:** `review.py` API from Phase 2 (`Draft`, `review(draft, revision)`, holds keyed by `source`, `Unreviewed` sends as written); guide tags `question`, `reply`, `all`.

**Acceptance gate:** Test lines for `scripts/production`, `scripts/hooks`, `scripts/notify` green; basedpyright 0/0; tests cover a held question failing registration, its rerun accepted, a reply missing the reviewed text blocked, an unrecorded `(recommended` line blocked.

### Phase 5 — Nightly messages pass  · status: todo

#### Work Order

**Goal:** Each night the review reads the day's verdicts and a sample of showrunner replies, queues at most one new guide rule for the user's yes or no, and counts how many replies that ask nothing would have failed.

**Spec:**
- `scripts/nightly_review/nightly_review.py` `MODES` gains `messages` (after `rust`); `commands/nightly_review.md` gains `<Scope mode="messages">`: read the night's `~/.local/state/notify/review.jsonl` lines and the last 24 h of showrunner replies (the transcripts of sessions `showrunners.py` lists, under the unit account's `projects/` directory); run `review.py replay`-style dry reviews over up to 20 replies that ask nothing (reply guide tags `reply,all`) and report how many fail and which rules; propose at most one new rule (stem, test, source quoting the user, a Bad/Good pair from tonight's messages) through the usual `DIR/messages.md` and ledger path, for `/nightly_next`.
- `review.py` gains `review.py sample <transcript-paths...> --limit N` for that dry review (posts nothing, holds nothing, logs `"sample": true`).

**Files:**
- `scripts/nightly_review/nightly_review.py`, `test_nightly_review.py`
- `commands/nightly_review.md`
- `scripts/notify/review.py`, `test_review.py`

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `review.py sample` and its tests; hub: `scripts/notify/review.py`
- `test` (opens as impl) — `nightly_review.py`, its test, `commands/nightly_review.md`

**Constraints from prior phases:** `review.jsonl` line shape from Phase 2; reply review uses guide tags `reply,all`; the question review of Phase 4 already covers replies that ask.

**Acceptance gate:** Test lines for `scripts/nightly_review` and `scripts/notify` green; basedpyright 0/0; `review.py sample` over two real transcripts prints verdicts and posts nothing.
