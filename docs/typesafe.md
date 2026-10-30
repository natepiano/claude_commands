# TypeSafe here

Use this guide from `~/.claude`. Ask `typesafe-unit` for anything it does not cover.

## Access and data

Run every live call as `with-typesafe <cmd>`. Never read, print, copy or log the key.
The wrapper supplies `TYPESAFE_API_KEY` to that process. If access is cold, ask the user to run `github-warmup`.
The user approved a $20 budget and sending issue titles, projects and bodies, including business, investor and legal notes, on the standard plan.
Style state includes rule names, summaries, crate and finding count. Send only the state needed for the question.
Current ratings, category, area, score, rank and dates are labels; never send them as issue state.
Get approval for a new data scope or more spend before making those calls.

## Reuse the client and question pattern

Use [client.py](../scripts/issue_score/client.py), not another HTTP client or parser:

```python
from scripts.issue_score import client, questions, vault

response = client.system_one(vault.build_state(issue), questions.main_questions())
```

Here `issue` is a `vault.Issue`; run the caller under `with-typesafe`. `system_one(state, questions, model=questions.MODEL, *, ledger_path=client.SPEND_LEDGER)` validates typed answers, retries transient errors and reserves spend.
Catch `client.TypeSafeError` (including `BudgetExceeded`) as an error. A failed call is never an answer.
Keep exact payloads, probabilities, usage and the returned model, as [score_issues.py](../scripts/issue_score/score_issues.py) does.

[questions.py](../scripts/issue_score/questions.py) owns wording, criteria, option names, `MODEL`, `QUESTION_SET_VERSION`, `THRESHOLDS` and `JEV_FIELDS`.
Read those constants for current policy; do not copy a threshold or proposal-field list into another tool.
Bump the question version for adopted wording; retest a model change against the pinned model.
[records.py](../scripts/issue_score/records.py) owns saved shapes and `STATE_DIR` (`~/.local/state/issue-score/`).
[decide.py](../scripts/issue_score/decide.py) owns the shared sure rule; [evaluate.py](../scripts/issue_score/evaluate.py) uses it too.
`Supported(threshold)` permits sure answers, `Unsupported` keeps a field agent-only, and `ReportOnly` reports it without a sure gate.
Split distributions, disagreeing Choice orders and an unresolved goal dependency stay unsure. One exception: when the `area_fits` Noul is below `EVIDENCE_ABSENT`, area becomes `unfiled` with confidence `1 - fits`, judged by that Noul instead of the area Choice orders, and it is sure only when that confidence passes the area threshold (`is_sure()`); an `area_fits` between `EVIDENCE_ABSENT` and `EVIDENCE_PRESENT` keeps area unsure. Cached raw answers get new decisions under current policy.

## Question rules

Follow the [TypeSafe skill](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md):

- Ask one concrete judgment per question; put arithmetic, dates and action rules in code.
- Supply relevant facts with named state fields. Keep instructions literal and criteria consistent.
- Give each Score level a situation. Use separate Nouls when several statements can be true.
- Questions cannot see each other's answers. Batch shared state; ask dependent cases in advance and select the answer in code.
- Pair a forced Choice with a fit Noul when nothing may fit. Test both option orders.
- Inspect distributions. Confidence concentration does not establish accuracy; measure the gate on your labels.
- Compare wording variants over the same state. Save payloads and inspect wrong cases before changing wording.

Use the live [API](https://docs.typesafe.ai/api/system-one), [confidence guide](https://docs.typesafe.ai/confidence), [skill guide](https://docs.typesafe.ai/agent-skill) and [documentation index](https://docs.typesafe.ai/llms.txt) for syntax and limits.

## New issues

[/issue](../commands/issue.md) runs:

```sh
with-typesafe scripts/lib/py scripts/issue_score/score_issues.py propose --project <short> --title '<title>' --body-file <utf8-file>
```

Omit `--body-file` for an empty body. Exit 0 prints one top value per field on the first line.
Use that line for `Proposed ranking:`. Keep unsure choices on the separate `Alternatives:` line so the user picks in the same reply.
Each unsure goal carries its own alignment. Preserve `split`, `check`, `possible 5` and derived `unfiled` hints; a real Choice of `unfiled` has no hint.
Use Jev only for `JEV_FIELDS`; the agent proposes the rest. On exit 2, show the stderr error and have the agent propose every field.
`vault.load_project_names()` reads `~/rust/hanadocs/home-base/issues - <short>.base`, the only shorthand source.
A new base adds a shorthand with no code change. A new canonical project also needs a `PROJECT_ABOUT` entry in `questions.py`.
Area definitions currently live in `~/rust/hanadocs/conf/issue-areas.md`: a two-column `area | definition` table.
This location is temporary. Counts live in `~/rust/hanadocs/home-base/issue areas.base`. Missing or empty definitions stop scoring; a missing area row keeps its name-only criteria.

## Score, compare and reassess

```sh
with-typesafe scripts/lib/py scripts/issue_score/score_issues.py score
scripts/lib/py scripts/issue_score/score_issues.py reassess <run>
```

`score` prints `~/.local/state/issue-score/runs/<run>.jsonl` and covers every open issue, including style, unrated and default-rated issues.
An error line holds `{"issue_path": "...", "error": "..."}`; failed scoring exits 1. A budget stop can leave other issues unscored.
Rerun `score` to reuse completed raw answers and retry missing ones. Keep the new run ID.
`reassess` reads saved answers without API calls, rebuilds decisions and writes these files under `~/.local/state/issue-score/reports/`:

1. Read `<run>-reassess.md`: current/Jev values, taken fields, user choices, missing style rules, failed and unscored lists.
2. Fill `<run>-agent-proposals.json`: retain `run_id`, each `issue_path` and `content_hash`; fill its `fields` with the agent's non-Jev choices.
3. Rerun `reassess <run>`. Filled choices survive. Repeat until no row awaits agent proposals; unrated/default-rated rows then have provisional scores and bands.
4. Confirm coverage: the report must have no failed or unscored issues. A partial report is marked partial; fix failures and rescore before claiming a full reassessment.
5. Review unrated/default-rated rows first, then walk user choices by `/prioritize` band. Check the score >= 20 section on its own.
6. Keep `<run>-reassess.json` unchanged as the snapshot. Put explicit user-approved values in an approvals file, one entry per issue:

```json
{"review_path":"<run>-reassess.json","approvals":[{"issue_path":"/absolute/path/to/issue.md","fields":{"backlog_impact":"⭐⭐⭐"}}]}
```

Save approvals beside the snapshot (or use an absolute `review_path`), then run:

```sh
scripts/lib/py scripts/issue_score/apply_labels.py <approvals-file>
```

Only approved goal, ratings, category and area change. Read them back; the watcher computes score and rank. Scoring never writes to the vault. The writer uses the run's saved vocabulary and definitions, checks the bytes it edits, and preflights the batch. Each file replacement is atomic.
Refusals print `issue-score: ...` on stderr and exit 2:

| Message includes | Recovery |
| --- | --- |
| `content hash changed`, `area definitions changed`, or `rescore needed` | Rescore; regenerate the review and reconfirm approvals. Keep the old artifacts for reference. |
| `approved field ... changed since review` | Rerun reassess, read the new current value and reconfirm that field against the new snapshot. |
| `duplicate issue approval` | Combine that issue's fields into one approval entry. |
| `approval does not identify an open issue` | Check the path and review; leave closed issues out and regenerate the review when needed. |
| `Invalid approved value` or `not an approvable issue label` | Correct the field/value using the rubric and definitions. |
| JSON, file-access or frontmatter errors | Repair that input, retain the snapshot and retry. |
| `issue changed during approval` | Rescore the changed issue and reconfirm before retrying. |

If a batch stops after some writes, run the same approvals again. A field already at its approved value counts as done; other stale checks still apply.
A complete scoring run proves coverage, not a complete comparison cohort. For tuning use `compare <candidate> --baseline <baseline> --split tune`: both runs need complete eligible coverage, and share current labels read once.
Use `experiment --variants <json>` for paired wordings; use `score --fresh` for a repeat, then `compare <run> --repeat <repeat> --split tune`. Different wording/order definitions are not repeat pairs.
Select thresholds only on complete tune runs; validate configured values once on held-out. Compare excludes historical effort examples from both runs.

## Spend and interrupted calls

At the configured $0.042/M input tokens, a planning estimate of 3.6M tokens is about $0.15 per pass; state size and variants change the total. Read actual usage from `spend.jsonl`.
`spend.jsonl` records cost. `spend.pending.json` holds UUID-to-dollar reservations; killed processes leave them in place (fail closed).
`BudgetExceeded` can occur below $20 recorded spend: recorded + reserved + the next request's bound exceeds the cap. To recover, stop all TypeSafe callers, keep copies of both files, and compare these totals with service billing:

```sh
scripts/lib/py - <<'PY'
from scripts.issue_score import client
p = client.SPEND_LEDGER
print("recorded USD", client._spent(p))
print("reservations USD", client._pending(p.with_suffix(".pending.json")))
PY
```

Reconcile billed but unrecorded requests with `typesafe-unit` first; keep unresolved reservations. For a confirmed stale reservation, replace `STALE_ID` below with its UUID. Clear only that entry, under the client's lock and atomic writer:

```python
from scripts.issue_score import client
p = client.SPEND_LEDGER
with client._ledger_lock(p):
    pending_path = p.with_suffix(".pending.json")
    pending = client._pending(pending_path)
    pending.pop("STALE_ID", 0.0)
    client._write_pending(pending_path, pending)
```

Then rerun scoring. Do not delete the ledger or raise the cap to clear a reservation.

## Measured lessons

See `~/.local/state/issue-score/lessons.md` for runs and details. Goal/impact/alignment passed Phase 3; the blind test preferred Jev 12–8. Read `JEV_FIELDS` for the user's current verdict.
Concrete current-harm criteria reduced false urgency-4 answers 29 -> 9. Deliverable-based category criteria improved tune accuracy, but category failed held-out at .15 error; feature/enhancement/research remain confused.
Area definitions raised tune accuracy .51 -> .67 and passed held-out at .091 error before merges. Sharper category definitions made accuracy worse and were rejected.
116 -> 64 areas failed the 10% tune rule at every threshold (.95 error .105). With 62 areas, .95 met it: tune .083 error/30% taken; held-out .100/27% taken. Phase 6b records the user's area decision; consult current policy, not these historical thresholds.
Judge high-priority issues separately: global passes hid weak alignment and ordering in the score >= 20 group. Urgency's majority one-star class makes exact match misleading; inspect kappa and rare 4–5 cases. Effort remains a check.
