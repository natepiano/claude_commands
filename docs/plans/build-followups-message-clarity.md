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

### Phase 2 — The push reviewer  · status: todo

#### Work Order

**Goal:** Every phone push is read cold by a reviewer before it is sent, and goes out as written or rewritten. Until the push-gate phase wires `send.py`, nothing is held and the writing session does not see the rewrite; `pushover.py`'s hold path and its rewritten-text output are built and tested here.

**Spec:**
- New `scripts/notify/review.py`. Public surface used by `pushover.py`: `review(draft: Draft, revision: bool) -> Verdict | Unreviewed`, `held_for(source: str) -> HoldLookup`, `hold(...)`, `release(source)`, `log_review(...)`; commands `review.py due`, `review.py replay [--limit N]`, `review.py switch on|off|status`.
  - `Draft` (frozen dataclass): `source`, `priority`, `title`, `action: UserAction`, `body` (the body without the first line), `answerable: bool`.
  - Reviewer process: `claude -p --model <agent> --effort <effort> --system-prompt <guide + reviewer prompt> --json-schema <schema> --output-format json --tools "" --no-session-persistence --setting-sources ""` (confirm each flag with `claude --help` and drop one that does not exist, saying so), the draft as the prompt (title, action line or "no action", body, priority, and whether it is a revision), environment: start from the caller's, remove `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `CLAUDE_CONFIG_DIR` (a writer on another account must never lend its login), then apply `accounts.role_account("unit")` → `named_account_environment(dir)` for a `NamedAccount`, nothing more for `DefaultAccount`; model and effort from `agents_resolve_print message_review.reviewer` (`bash -c 'source ~/.claude/scripts/agents/agents_config.sh; agents_resolve_print message_review.reviewer'`), falling back to `opus` / `medium` when unresolved. Timeout 20 s, so review plus Pushover's 15 s post stays inside `send.py`'s 40 s wait for pushover. The process runner is injectable for tests.
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
- `send.py` passes no `--answerable` until the push-gate phase wires it, so until then every `ask` posts the reviewer's version at once (logged `rewritten, script text failed`) and nothing is held in practice; `pushover.py`'s hold path is built and tested here.
- Proposed lines for the showrunner (not committed): `config/agents.conf` `[assignments]` gains `message_review=claude`, and a new section `[message_review.claude]` with `reviewer=opus:medium` (unit director's call: medium keeps a review near 10 s; `/agent` can raise it).

- Named types (no bare optional in the public surface): `HoldLookup = NoHold | LiveHold(hold: Held) | UnreadableHold(path, why)`; an unreadable hold file moves to `held/bad/`, is logged, and counts as `NoHold`. `ReviewOutcome = Approved(draft) | Rewritten(version, failed_rules) | Questions(best_version, questions, failed_rules) | Unreviewed(why)`; nullable JSON fields convert to these at the boundary. `Held` carries `source` (who must answer), `created`, `draft`, `best_version`, `questions`.
- Hold delivery has one owner. Under the lock, `due` or a revision claims a hold by renaming `<id>.json` to `<id>.sending`; only the claimer posts. A failed post on expiry renames it back to `.json` and logs `expired send failed: <why>`; the next `due` retries, so the push is never lost. If writing the hold or registering the notifier job fails, post the best version at once and log `hold failed: <why>`.
- `review.py check --title T (--action A | --no-action) --text B [--priority N]`: a dry review of one supplied draft; prints the verdict, failed rules and the reviewer's version; posts nothing, holds nothing, logs `"dry": true`. The gate and the push-gate phase use it.
- Caller budgets: `scripts/whoami/quota_alert.py` and `scripts/buildlog/rust_release.py` wait 30 s on their `--to user` send; raise each wait to 60 s (also touches; owners named in the notice). Test that a reviewer timeout still sends as written through `pushover.send()` within 40 s.
- Guide: add `push` to `tags` in `~/rust/nate_style/message/every-choice-says-what-it-does.md` (nate_style worktree `/home/natepiano/worktrees/nate_style-message-clarity`, branch `message-guide`; the showrunner merges it), so `--tags push,all` gives 11 rules.

**Files:**
- `scripts/notify/review.py` — new
- `scripts/notify/reviewer_prompt.md` — new
- `scripts/notify/test_review.py` — new
- `scripts/notify/pushover.py` — the review step, `--answerable`, exit 4
- `scripts/notify/test_pushover.py` — review outcomes
- `commands/message_review.md` — new
- `scripts/whoami/quota_alert.py` — 60 s wait on the user send
- `scripts/buildlog/rust_release.py` — 60 s wait on the user send
- `~/rust/nate_style/message/every-choice-says-what-it-does.md` — `push` tag

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `scripts/notify/review.py`, `reviewer_prompt.md`, `pushover.py`, `commands/message_review.md`; hub: `scripts/notify/pushover.py` (the exit-4 and stdout contract `send.py` parses)
- `test` (opens as impl) — `quota_alert.py`, `rust_release.py`, the guide tag, `scripts/notify/test_review.py`, `test_pushover.py` (fake process runner, fake `post`, temp state dir via env or patched paths: send, rewrite, ask+answerable holds and exits 4, ask+script posts at once, revision never held, timeout/bad JSON/non-zero exit → sent as written and logged unreviewed, switch off, due after 300 s, action-form checks)

**Constraints from prior phases:** This phase edits no `scripts/message/` file: send.py's side of the review moved to the push-gate phase, which waits on shutdown-unit's Phases 8–9 (unit director's split, 2026-10-13). `scripts/notify/pushover.py` and `test_pushover.py` are shutdown-unit's files (also touches; its Phases 8–9 do not edit them). The guide is 13 rule files in `~/rust/nate_style/message/` plus `README.md` (merged to nate_style main as 4aaf1f1 and live on both machines), loaded by `scripts/ux_style/load-ux-style.sh --guide <dir>` (`--tags push,all` keeps each rule carrying any listed tag; `zsh scripts/ux_style/load-ux-style.sh --guide <dir> --tags push,all --checklist` prints 11 lines once the tag is added). Rule tags in use: `all` (5 rules), `push` (5), `question` (3), `reply` (2). `/message_style` (`commands/message_style.md`) loads the whole guide for a writing session; `/alert_user`, `/message`, produce.md `<Notify>`, `/build:report` and `/fix` load it before their first message to the user.

**Acceptance gate:** both Test lines green; Test lines for `scripts/whoami` and `scripts/buildlog` green; basedpyright 0/0 on changed files; `review.py replay --limit 5` (unit director, live: reviewer runs, nothing posts) prints five verdicts; `review.py check` fed the 2026-10-13 02:19 EDT push "Hana: nested-g2 wrap" (text taken from `pushover.jsonl`) returns a rewrite or questions, and its rewrite passes the guide's checklist by the director's reading; `grep -c '"sent"' ~/.local/state/notify/pushover.jsonl` unchanged across the run.

### Phase 3 — send.py's review outcomes, the built-in push gate and every script's fixed push text  · status: todo

#### Work Order

**Goal:** A session's push can be held with questions and its writer sees exactly what was sent; Claude's built-in PushNotification works only right after a failed send and carries the reviewed text; every script's fixed push text passes the guide.

**Spec:**
- `send.py`:
  - `answerable` is true when the send has no `--from` and `default_sender()` resolved a session name; `user()` passes `--answerable` to pushover then. `remote()` forwards `--answerable` for a `--to user` send that is answerable here, and `send.py` accepts it as an internal flag (`--answerable`, only with `--to user`).
  - Pushover exit 4 → new outcome `held` (printed `HELD: <the questions, all lines, untruncated>`, exit 4); `remote()` recognises `HELD`. Exit 0 with `sent as rewritten:` → `SENT: to the user, rewritten:` followed by the sent text lines (not passed through `one_line`).
- `commands/message.md` "Outcomes and recovery": add `HELD: <questions>` (exit 4): the reviewer held your push; send a revision with the answers within 5 minutes (the same `--summary` is not required); a revision is never held again; with no revision the reviewer's version goes out at 5 minutes. And: `SENT: to the user, rewritten:` shows exactly what the user received.
- On `FAILED` for a `--to user` send (pushover exit 1 after review), `send.py` writes `~/.local/state/notify/failed/<session_id>.json` (`time`, the reviewed title/action/body that failed to post) when it runs under a Claude session (session id from the `~/.claude/sessions/$CLAUDE_PID.json` record), and its `FAILED:` output ends with `fallback text:` and the reviewed text, so the fallback carries it. `pushover.py` prints the reviewed text on its failure path for `send.py` to pass on.
- New `scripts/hooks/pre-tool-use-push-gate.py` (PreToolUse, matcher `PushNotification`): reads the hook payload's `session_id`; allows when `failed/<session_id>.json` is under 10 minutes old and deletes it; otherwise denies with: `PushNotification is only the fallback for a failed send. Send with ~/.claude/scripts/message/send.py --to user (see /alert_user); it is reviewed for clarity first.` Any hook error allows (a push is never lost) and prints the error to stderr. Test file `scripts/hooks/test_push_gate.py`.
- Proposed `settings.json` lines for the showrunner (not committed): a `PreToolUse` entry `{"matcher": "PushNotification", "hooks": [{"type": "command", "command": "~/.claude/scripts/lib/py ~/.claude/scripts/hooks/pre-tool-use-push-gate.py"}]}` (copy the exact command form the existing `mcp__brp__brp_launch` entry uses).
- `commands/alert_user.md` and produce.md `<Notify>`: the `FAILED` fallback line becomes "fall back to PushNotification with the `fallback text:` that `send.py` printed".
- Fixed push texts: read each script's user-bound text once against the guide's `push` and `all` checklist, and fix every one that fails where it is written: `scripts/buildlog/rust_release.py`, `scripts/lint/sweep.py`, `scripts/production/ci_points.py`, `scripts/production/codex_winddown.py`, `scripts/shutdown/{record,restart,settle,shutdown}.py`, `scripts/whoami/quota_alert.py`, `scripts/message/escalate.py` (only the text it composes). Each sends with `--from <script name>` (add it where missing, so the reviewer never holds a script). Their tests that assert the old text change with it.
- Unit director's note in the phase notice: each script file's owner unit, so the showrunner can tell them.

- Remote outcomes: `send.py`'s output is a header line (`SENT|HELD|FAILED|REFUSED|SKIPPED|QUEUED: …`) followed by payload lines; `remote()` finds the last header line in the ssh output and keeps every line after it, so `HELD` questions, rewritten `SENT` text and `fallback text:` survive the hop. The failure marker is written by the originating machine's `send.py` for its own session after `remote()` returns `FAILED`; the far side never writes one.
- A marker is written for every user-delivery failure: pushover exit 1, exit 2 without `refused before sending:` (missing keys), and a pushover timeout. A refusal writes none. The marker holds the reviewed title and message.
- The push gate allows only when the PushNotification's title and message match the marker's reviewed text (whitespace-normalised), then deletes the marker; a mismatch denies and keeps it. Tests: mismatched content denied, a second use denied.
- Fallback lines: `commands/build/report.md`, `commands/fix.md`, `commands/watcher.md` and `commands/message.md` say a failed send to the user falls back to PushNotification with the `fallback text:` `send.py` printed; `/message` keeps "no fallback" for sends between sessions.

**Files:**
- `scripts/message/send.py` — `HELD`, rewritten `SENT`, `--answerable`, failure marker and `fallback text:`
- `scripts/message/test_send.py` — those, plus `HELD`, rewritten `SENT` and `--answerable`
- `commands/message.md` — `HELD` and rewritten `SENT` outcomes
- `scripts/notify/pushover.py` — prints the reviewed text on failure
- `scripts/notify/test_pushover.py` — that
- `scripts/hooks/pre-tool-use-push-gate.py` — new
- `scripts/hooks/test_push_gate.py` — new
- `commands/alert_user.md` — fallback line
- `commands/showrunner/produce.md` — `<Notify>` fallback line
- `scripts/buildlog/rust_release.py` — push text, `--from`
- `scripts/lint/sweep.py` — push text
- `scripts/production/ci_points.py` — push text, `--from`
- `scripts/production/codex_winddown.py` — push text
- `scripts/shutdown/record.py` — push text
- `scripts/shutdown/restart.py` — push text
- `scripts/shutdown/settle.py` — push text
- `scripts/shutdown/shutdown.py` — push text
- `scripts/whoami/quota_alert.py` — push text
- `scripts/message/escalate.py` — the text it composes
- `scripts/buildlog/test_rust_release.py` — where the old text is asserted
- `scripts/lint/test_sweep.py` — where the old text is asserted
- `scripts/production/test_ci_points.py` — where the old text is asserted
- `scripts/production/test_codex_winddown.py` — where the old text is asserted
- `scripts/shutdown/test_record.py` — where the old text is asserted
- `scripts/shutdown/test_restart.py` — where the old text is asserted
- `scripts/shutdown/test_settle.py` — where the old text is asserted
- `scripts/shutdown/test_shutdown_cli.py` — where the old text is asserted
- `scripts/whoami/test_quota_alert.py` — where the old text is asserted
- `scripts/message/test_escalate.py` — where the old text is asserted
- `commands/build/report.md` — fallback line
- `commands/fix.md` — fallback line
- `commands/watcher.md` — fallback line

**Seats:** 2 writers (no test lane; `test` opens as impl)
- `impl` — `send.py`, `pushover.py`, the push gate hook and their tests, `commands/message.md` and the fallback-line edits in `alert_user.md`, `produce.md`, `build/report.md`, `fix.md`, `watcher.md`; hub: `scripts/message/send.py`
- `test` (opens as impl) — the ten scripts' fixed texts and their tests

**Constraints from prior phases:** Before dispatch, check each of the ten scripts against its owner's branch and worktree for unmerged edits; `scripts/shutdown/*` and `scripts/message/send.py` land only after shutdown-unit's Phases 8–9 merge (showrunner, 2026-10-13), else tell the showrunner which file blocks. Phase 2 built `pushover.py`'s `--answerable` flag and hold path but nothing passes the flag yet; this phase's `send.py` does. Phase 2's contract: pushover exit 0 sent (stdout `sent as rewritten:` + lines on a rewrite), 1 post failed, 2 refused, 4 held; `send.py` outcomes add `HELD` (exit 4); scripts with `--from` are never held (their failed text is rewritten at once and logged `rewritten, script text failed`); `review.py replay` reviews without posting.

**Acceptance gate:** Test lines for `scripts/notify`, `scripts/message`, `scripts/hooks`, `scripts/buildlog`, `scripts/lint`, `scripts/production`, `scripts/shutdown`, `scripts/whoami` green; basedpyright 0/0; the hook test covers allow-after-failure, deny otherwise, marker consumed, hook error allows; `review.py check` over each changed script's new text returns `send`.

### Phase 4 — Questions in showrunner replies  · status: todo

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

### Phase 5 — Nightly messages pass  · status: todo

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

**Constraints from prior phases:** `review.jsonl` line shape from Phase 2; reply review uses guide tags `reply,all`; the question review of Phase 4 already covers replies that ask.

**Acceptance gate:** Test lines for `scripts/nightly_review` and `scripts/notify` green; basedpyright 0/0; `review.py sample` over two real transcripts prints verdicts and posts nothing.
