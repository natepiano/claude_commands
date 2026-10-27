# typesafe

> **Status: IMPLEMENTATION PLAN — phased, direct-ready; design up for the user's review.** A script that has TypeSafe's Jev score each hanadocs issue field, measured against the values the vault holds now, and then used by `/issue` to propose fields for a new issue.

> **Production: build-followups** — unit `typesafe-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-26 20:16 AST

research how to use typesafe - i want you to become a resource for other sessions on its use. your first task will be to create a script that will score each of the fields we use for issues - i want a plan for how it works first - so we can review it. also i want to use typesafe on creation of new issues so it can appropriately cateogorize fields for an issue. we'll need to come up with appropriate prompts and thresholds but for now i just want you to use the skill to research how to approach these - as well as search the web - and come up with a plan and let natedev know when the plan is ready to review

## Design — how the scorer works

### What the vault holds now

Measured 2026-10-26 on natedev's `~/rust/hanadocs`:

- 543 open issues. 96 of them are style-rule proposals (`tags: style`, `style_rule:` set). The style production writes those in bulk with fixed ratings and an empty body, so the scorer skips them.
- That leaves 447 open issues, 40 of them unrated. About 70 closed issues also carry ratings. Together these give about 480 rated issues to measure against.
- 163 open issues have a body under 200 characters, so the title is almost all the state. The evaluation reports these as their own group.
- The values are skewed. Goal is `1 - Ship Hana` on 81% of rated issues, and urgency is one star on 86%.
- `area` has 118 values, and some are spelling variants of each other (`tool`/`tooling`, `ui`/`"[[ui]]"`). `category` is single-valued on all but 3 issues.
- Bodies carry Obsidian wikilinks that were added automatically (`[[tool]]`, `[[issue structure#example|example]]`). The model sees these as noise.
- An agent proposed each rating and the user confirmed it, in `/issue` or `/prioritize`. `/prioritize` also re-rates alignment and impact *against the other issues in a band*, while Jev rates each issue alone. So expect less agreement on alignment and impact in the score ≥ 20 band.

### What TypeSafe gives us

Jev answers typed questions about state we send. A **Noul** gives the probability that a statement is true. A **Choice** picks one option and gives a probability for each. A **Score** gives a probability for each described level, plus `score`, which is the probability-weighted mean. Jev reads each question alone: questions in one request cannot see each other's answers. Output is free, and input costs $0.042 per million tokens. The docs list these weak spots: counting, arithmetic, date comparison, large state full of detail that does not matter, a bias toward the first Choice option, and literal reading of the words. Confidence measures how concentrated an answer is. It is not a calibrated chance that the answer is right, so we calibrate on our own labels.

The governing rule, from the skill: **Jev makes the judgments, and code makes the decisions.** Each field is one or more atomic questions, and a table in code maps the answers to a star value or option.

### One request per issue

**State** (the same for every question in the request):

```json
{"issue": {"title": "...", "project": "hana_diegetic",
           "project_about": "one line: what this project is",
           "body": "body with wikilinks reduced to their display text"}}
```

- `project_about` comes from a table in `questions.py`, one line per project, written from each repo's README. It supplies facts Jev does not have, for example that `cargo-port` is a contributor tool and not the product.
- Never sent: the current ratings, category, area, rank, stage or dates. They are the labels, or they are noise.
- The body is capped at 24,000 characters, cut at a heading. Only a few issues reach the cap. The 32k-token limit for one question is far above it.

**Questions:**

| Field | Questions | Code's decision |
| --- | --- | --- |
| `backlog_goal` | One Choice over `ship_hana`, `find_collaborators`, `seek_investors`, `business_viability`, with each goal's definition from `prioritization goals.md` as its criterion. It is asked twice in the same request, in the listed order and in reverse, because Jev favors the first option. | Average the two distributions and take the top goal. If the two orders pick different goals, the answer is unsure. |
| `backlog_alignment` | Four Scores, one per goal: "How strongly does `issue` advance this goal: …?". The five rubric levels are each written as a situation. | Read the Score for the chosen goal. This is a speculative fan-out: the alignment question depends on the goal answer, and questions cannot see each other's answers. |
| `backlog_impact` | One Score: "How large is the benefit to Hana or its users once the work in `issue` is done?". Each level is a situation, and the criteria say not to count how soon or how hard the work is. | Median level (see below). |
| `backlog_urgency` | Four Nouls for evidence: `deadline_named` (a date or event the work must meet), `outside_cutoff` (an outside offer or opportunity that ends), `current_harm` (something broken now that harms users, data or other work), `blocks_planned_work` (other planned work cannot start). One 3-level Score for pressure: can wait / pressure building / pressing. | If no evidence Noul is above 0.7, stars = 1 + the pressure level, at most 3. If any evidence Noul is above 0.7, propose 4. **The tool never proposes 5**: five stars puts an issue at the top of the backlog, and a hard cutoff needs date math Jev cannot do. It flags "possible 5" instead. A Noul between 0.3 and 0.7 makes the answer unsure. |
| `backlog_effort` | One Score with the five XS–XL levels. Each level is a situation with two short examples taken from real issues. | Median level, always marked "check". Published studies find effort estimates from issue text weak: GPT2SP's gains came from an evaluation bug, and Deep-SE beat a median baseline in 8 of 42 cases. Phase 3 measures whether ours does better. |
| `category` | One Choice over the nine categories, asked in both orders. The criteria name the cases people mix up: an enhancement improves something that exists, while a feature adds something that does not; a refactor changes no behavior; research has a question to answer, while an idea is not yet actionable. | Top option after averaging. One category, matching 99% of the vault. |
| `area` | **A second request**, so the area list does not mislead the other questions. State: the issue plus `areas`, the normalized vocabulary (about 110 values). Questions: a Choice over the areas in two orders, plus a Noul: "Does one of the areas in `areas` name the part of the work `issue` is about?". | If the Noul is below 0.3, propose `unfiled` and show the top area as a hint. Otherwise take the top area. |

`project` is not scored: the `/issue` shorthand gives it. `depends_on` is not scored either. It links one issue to another, which means searching all the issues, and that is a separate tool.

### Reading the answers

- **The stored star value is the median level** of the Score distribution. The median minimizes absolute error, and the vault stores whole stars. The probability-weighted mean (`score`) is kept for ranking and for the evaluation.
- **Split answers.** Two levels that are not neighbors, each at 0.25 or more, mark the answer split. A split answer is always unsure.
- **Sure or unsure.** An answer is sure when the probability of its proposed level or option is at or above that field's threshold, nothing is split, and the two orders agree. Every field starts at 0.6. Phase 3 replaces these values with ones measured on a risk-coverage curve.
- Failed API calls raise errors. A failed call never becomes a value.

### Evaluation — the `compare` report

- **Labels:** the ratings of about 480 rated, non-style issues. A hash of each file name splits them, the same way on every run, into a 70% tuning set and a 30% held-out set. The held-out set is scored once, after tuning ends.
- **Ordinal fields:** exact match, within one star, MAE, quadratic-weighted kappa, ranked probability score and a 5×5 confusion matrix. Each is shown for the whole set, for title-only issues and for the score ≥ 20 band.
- **Choice fields:** accuracy, a confusion matrix, Brier score, and reliability by confidence bucket.
- **Ordering:** apply `renumber.py`'s score formula to the Jev stars, and report the Spearman correlation with the current `backlog_score`. The formula is imported, not copied. Ordering is what the ratings are for.
- **Risk-coverage table** for each field: at each threshold, the share of issues marked sure and the error rate among them. Choose the lowest threshold that meets that field's error target on the tuning set, then check it on the held-out set.
- **Repeat check:** score the tuning set twice and report how often a value changes. The TypeSafe self-consistency cookbooks show small changes between runs.
- **Disagreement list:** for each field, the 20 issues with the largest gap between Jev and the vault, with title and both values. Some of these will be wrong labels and not wrong answers. The user reads them.

### Using it in `/issue`

`score_issues.py propose --project <short> --title … --body …` prints one line in `/issue`'s existing format, for example `goal 1 - Ship Hana · alignment ⭐⭐⭐ · impact ⭐⭐⭐ · urgency ⭐ · effort ⭐⭐ (check) · category feature · area rendering`. A field that is unsure shows its top two values. `/issue` step 4 uses this line as its proposal, and the user still confirms in one reply, as now. A proposal filed by a unit puts the same line on its `Proposed ranking:` line. If the API fails, `/issue` says so and proposes the values itself, as it does today. Only the fields the user approves after Phase 3 use Jev. The others stay with the agent.

### Code layout

- New folder `scripts/issue_score/`, Python standard library only, like the rest of the repo. The HTTP API is one POST, so the client uses `urllib` with backoff on 429/529, not the SDK. That avoids a virtualenv, which no checkout here has.
- The model is pinned to `jev-1.14.0`, not the `jev-latest` alias, because the thresholds are tuned against one version. Each result records the model that answered.
- All question wording, criteria, option names, thresholds and project lines live in `questions.py`. That file is the one people audit and edit.
- A spend ledger, `~/.local/state/issue-score/spend.jsonl`, logs each request's input tokens and cost. The client refuses a request once the total reaches the cap, `BUDGET_USD = 20.0` in `questions.py`.
- Results go to `~/.local/state/issue-score/`: one JSONL line per issue per run, holding the exact request payload, every probability and the model ID, so any case can be rerun. A run reuses a cached result when the issue text, the question set version and the model are unchanged.
- The tool never writes to the vault. Ratings change only through `/issue` and `/prioritize`, with the user's confirmation, as now.

### Cost, key, data

- One full pass is about 450 issues × about 4k input tokens (state plus about 25 questions) × 2 requests, or about 3.6M tokens. That costs about $0.15. Phase 3 tuning should stay under $5.
- The key is already on both machines, encrypted at `~/.config/typesafe/token.gpg` (nixos a289e23, `modules/common/typesafe.nix`). Every live call runs as `with-typesafe <cmd>`. That nixos command decrypts the key through gpg-agent and sets `TYPESAFE_API_KEY` for that one command only. The scorer reads only that variable. No code, log, payload or message reads, prints or stores the key. If the variable is missing, the scorer stops with an error that names `with-typesafe`. If gpg-agent is cold, `with-typesafe` fails, and the user runs `github-warmup`.
- The data sent is the title and body of each non-style issue, including the business, investor, legal and financial notes. TypeSafe states that it does not train on requests. Zero data retention is offered only on enterprise plans.

### Questions for the user (needed before Phase 1's live call)

1. A go to send issue titles and bodies to TypeSafe on a standard plan. The key is in place.
2. A budget for experiments. The proposal is $20, about four times the expected need.
3. A human ceiling. Studies find a large share of human issue labels are wrong; Herzig et al. found 33.8% of issue reports misclassified. Will you re-rate 30 issues blind, about 20 minutes, so we know how often you agree with your earlier ratings? Recommended: yes. Without it, a Jev score of "70% exact" has nothing to compare against.

### Review decisions (the user, 2026-10-26, /adhoc_review)

1. **Data: send all of it.** The title, project and body of every non-style issue may go to TypeSafe on a standard plan, business, investor and legal notes included. The user added: "we did create a bunch of proposals at a deafult level (which is still happening because we don't have jev in use yet) so we can't use those to determine anything". Default-rated proposals are therefore not labels, and they are left out of tuning and evaluation.

   The style unit's answer (2026-10-26) fixes the label filter in `vault.py`:
   - No code writes defaults into frontmatter. A style proposal is filed with the five fields empty and a `Proposed ranking:` body line. That line comes from a formula in `scripts/style/style_proposals.py` (`proposed_ranking()` ~:204, `project_goal()` ~:189):
     - goal: the project's most common goal among open non-style issues, falling back to `1 - Ship Hana`;
     - alignment ⭐⭐;
     - impact ⭐ for a rename, ⭐⭐ otherwise;
     - urgency ⭐;
     - effort ⭐ for ≤ 3 findings, ⭐⭐ for ≤ 15, ⭐⭐⭐ for more.
   - The `stars` verb of `scripts/production/proposal_issue.py` writes the five fields when the user confirms a proposal on review, and deletes the line. Empty fields plus a `Proposed ranking:` line mean not rated.
   - All 86 rated style issues match the formula exactly. Frontmatter cannot tell an accepted proposal from a copied default.
   - **Rule:** a style issue (`style_rule:` or `style_rules:` set) is a label only when its values differ from the formula. Every other style issue is left out.
   - A non-style issue with a `Proposed ranking:` line is unrated (12 open now): its values are the filer's judgment, not yet confirmed. Non-style proposals rated through `stars` (tool-based-ui, research-unit and others) were confirmed by the user, so they count as labels.
   - The user, after this answer: "once we get jef working - we should apply it to the defaulted and missing - and in fact i want to reassess all of our issues in case i like jev's categorization better". So the plan adds a reassessment phase (Phase 5) that scores every open issue, including style issues, issues with default ratings and unrated issues. Style issues need their own state, built from their rule fields because their body is empty.
2. **Budget: $20.** The user: "i am happy to add $20 to jev". The scorer keeps a running total from each response's `usage.input_tokens` at $0.042 per million. It stops before a request that would pass $20, and asks the user before it spends more.

### Resource for other sessions

`typesafe-unit` stays on to answer other sessions' TypeSafe questions. Phase 5 writes `docs/typesafe.md` so the knowledge outlasts this session: how to run a command under `with-typesafe`, how to reuse the client, the `questions.py` pattern, and the measured lessons from Phase 3. Those lessons cover which wordings won, the order-bias results, threshold values, and where Jev was wrong.

## Delegation Context

- **Project:** `~/.claude` — the user's Claude Code configuration; this unit adds `scripts/issue_score/`, a TypeSafe-backed scorer for hanadocs issue fields.
- **Stack:** Python ≥ 3.10, standard library only, run through `~/.claude/scripts/lib/py`; TypeSafe HTTP API `POST https://api.typesafe.ai/v1/systemone`, model `jev-1.14.0`; schemas at `https://api.typesafe.ai/openapi.json` and `https://docs.typesafe.ai/api/system-one.md`.
- **Layout:**
  - `scripts/issue_score/` — new: `client.py`, `vault.py`, `questions.py`, `decide.py`, `evaluate.py`, `score_issues.py` (CLI), `test_*.py`, `fixtures/`
  - `commands/issue.md` — `/issue`; step 4 gains the `propose` call (Phase 4)
  - `docs/typesafe.md` — new guide (Phase 5)
  - `pyrightconfig.json` — one execution-environment entry for `scripts/issue_score`
- **Key files:**
  - `~/rust/hanadocs/issues/*.md` — issues, read only; skip any with `style_rule:` in frontmatter, skip directories
  - `~/rust/hanadocs/prioritization goals.md` — goal definitions, the goal Choice criteria
  - `~/rust/hanadocs/issue structure.md` — category definitions and the star rubric
  - `commands/issue.md` — the rubric levels for alignment, impact, urgency and effort (step 4)
  - `scripts/prioritize/renumber.py` — score formula (lines ~420–436: `4*(alignment-1) + 3*(impact-1) + URGENCY_BONUSES[...] - (effort-1) + goal bonus`); imported by `evaluate.py`, never copied
- **Test lanes:** `scripts/issue_score/test_*.py`; tests never call the live API, they replay recorded responses from `scripts/issue_score/fixtures/`
- **Build:** none
- **Test:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only`
- **Lint:** `basedpyright scripts/issue_score` — pass is its `0 errors, 0 warnings` line
- **Invariants:**
  - The scorer never writes inside `~/rust/hanadocs`.
  - A failed API call is an error, never an answer.
  - All question text, criteria, option names and thresholds live in `questions.py`.
  - Current field values are never sent in the state.
  - Live API calls only after the user's go (Questions for the user 1–2), only within the budget, and always as `with-typesafe <cmd>`.
  - The key is never read, printed, logged or stored. Code sees it only as `TYPESAFE_API_KEY` in its own environment.

## Phases

### Phase 1 — client, state and questions; the `score` command  · status: todo

#### Work Order

**Goal:** `score_issues.py score [--only <glob>] [--limit N]` scores open non-style issues and writes one JSONL result per issue to `~/.local/state/issue-score/runs/<run-id>.jsonl`.

**Spec:**
- `client.py`: `system_one(state, questions, model) -> Response`. Uses `urllib`, gzip body, and `Authorization: Bearer <key>`. The key comes only from `TYPESAFE_API_KEY`, which `with-typesafe` sets. If it is missing, raise `TypeSafeError("TYPESAFE_API_KEY not set: run under with-typesafe")`. The client never decrypts the key itself, and it never puts the key in a log, a payload record or an error message. Before each request, it checks the spend ledger against `BUDGET_USD` and raises `BudgetExceeded` if the total has reached the cap. After each response, it appends the input tokens and cost to the ledger. It retries 429/529 with exponential backoff, following `retry-after` when present, for at most 5 attempts. Any other non-2xx raises `TypeSafeError` with the status and body. Typed `TypedDict`s describe the Noul, Choice and Score answers, following the response shapes on the API page. No `Any`.
- `vault.py`: `Issue` dataclass (path, title, project, body, labels), with labels parsed from the frontmatter: goal, the four stars, category, area. `load_open_issues()` skips style issues and directories. `load_rated_issues()` also skips any issue with a `Proposed ranking:` body line, and it keeps a style issue only when its five values differ from the style formula (Review decisions 1). It returns each skipped path with its reason, so the report can show the count. `clean_body()` reduces `[[a|b]]` to `b` and `[[a]]` to `a`, then applies the 24,000-character cap at a heading. `normalize_area()` strips quotes, `[[ ]]` and case. `area_vocabulary()` returns the sorted, deduplicated normalized areas of non-style issues.
- `questions.py`: `QUESTION_SET_VERSION`; `PROJECT_ABOUT` (one line per project in `/issue`'s mapping table, from each repo's README); and builders for the main request and the area request, exactly as in the Design table. Option names describe content. Levels are situations. Each Noul's `false` criterion is a concrete situation. The five rubric levels come from `commands/issue.md` step 4, rewritten as situations.
- `decide.py`: median level; split detection (two non-neighbor levels each ≥ 0.25); averaging of the two-order Choice; the urgency table, which never returns 5 and sets a `possible_five` flag; and `sure`/`unsure` per field from `THRESHOLDS` in `questions.py`, every field starting at 0.6.
- `score_issues.py score`: runs the main and area requests per issue, 8 issues at a time. The result line holds the issue path, a content hash, `QUESTION_SET_VERSION`, the model that answered, both payloads, every answer and the decided fields. It reuses a cached line when the hash, version and model all match.

**Files:** `scripts/issue_score/{client,vault,questions,decide,score_issues}.py`, `scripts/issue_score/test_{client,vault,decide,score_issues}.py`, `scripts/issue_score/fixtures/*.json`, `pyrightconfig.json` (one entry).

**Workers:** `1 writer + 1 tester`
- `impl` — all `scripts/issue_score/*.py` except the tests, and `pyrightconfig.json`
- `test` — `test_*.py` and `fixtures/`, written from the Spec: retry on 429 then success; a 500 raises; wikilink cleaning; style issues skipped; median and split cases; the urgency table rows, including "never 5"; the cache hit.

**Constraints from prior phases:** none.

**Acceptance gate:** the merge gate is green; `basedpyright scripts/issue_score` prints `0 errors, 0 warnings`; and, once the user has given the go, `with-typesafe ~/.claude/scripts/lib/py scripts/issue_score/score_issues.py score --limit 5` writes 5 result lines on the live API, and the unit director reads each one.

### Phase 2 — the `compare` report  · status: todo

#### Work Order

**Goal:** `score_issues.py compare <run-id> [--split tune|holdout|all]` writes a Markdown report with every metric in the Design's Evaluation section, and the baseline run on the tuning set exists.

**Spec:**
- `evaluate.py`: split by `sha1(filename) % 10 < 7` → tune. Ordinal metrics: exact, within one, MAE, quadratic-weighted kappa, ranked probability score, confusion matrix. Choice metrics: accuracy, confusion, Brier, reliability in 5 confidence buckets. Groups: all, title-only (body under 200 characters after cleaning), and score ≥ 20 (the current `backlog_score`). Spearman correlation between the composite score from the Jev stars, using `renumber.py`'s formula through an import, and the current `backlog_score`. A risk-coverage table per field at thresholds 0.40–0.95 in steps of 0.05. Repeat-check rate when given two run IDs. The 20 largest disagreements per field.
- The report goes to `~/.local/state/issue-score/reports/<run-id>-<split>.md`.

**Files:** `scripts/issue_score/evaluate.py`, `scripts/issue_score/score_issues.py` (the `compare` subcommand), `scripts/issue_score/test_evaluate.py`.

**Workers:** `1 writer + 1 tester` — `impl` owns `evaluate.py` and `score_issues.py`; `test` writes `test_evaluate.py` with hand-computed kappa, ranked probability score and Spearman cases.

**Constraints from prior phases:** set by phase review.

**Acceptance gate:** the merge gate is green; basedpyright clean; a baseline `score` run on the tuning set, with its `compare` report, exists and the unit director has read it.

### Phase 3 — tune wordings and thresholds; the user's review  · status: todo

#### Work Order

**Goal:** tuned `questions.py` and measured thresholds, and a held-out report the user reviews field by field.

**Spec:**
- Work on the tuning set only. To compare wordings, send each variant as a separate question in the same request, over the same state, then compare the variants with `compare`. When an answer looks wrong, check in this order: the payload, the state, the options, the question's scope, then the wording.
- Watch for prompt gymnastics: no criteria that name test issues, no clause added per failure, no thresholds fitted to a few cases.
- For each field, take the threshold from its risk-coverage table, using the lowest value whose error meets the target. Targets: goal and category 10% error; impact and alignment within one star on 90%; urgency and effort have no target and are reported only.
- Bump `QUESTION_SET_VERSION`, then score the held-out set once. Write the held-out report, and run the repeat check on the tuning set.
- Send the user the report, the per-field recommendation (Jev proposes / agent proposes), and the disagreement lists. The user's verdict per field is recorded in `questions.py` as `JEV_FIELDS`.
- Record the lessons in `~/.local/state/issue-score/lessons.md` for Phase 5.

**Files:** `scripts/issue_score/questions.py`, and `scripts/issue_score/test_decide.py` when thresholds move.

**Workers:** `1 writer` — the unit director runs the experiments; the writer applies the chosen wordings and thresholds.

**Constraints from prior phases:** set by phase review.

**Acceptance gate:** the merge gate is green; the held-out report exists; the user's per-field verdict is recorded in `JEV_FIELDS`.

### Phase 4 — `/issue` proposes with Jev  · status: todo

#### Work Order

**Goal:** a new issue's proposal line comes from Jev for every field in `JEV_FIELDS`, and the user still confirms in one reply.

**Spec:**
- `score_issues.py propose --project <short> --title <t> [--body-file <f>]` prints the one-line proposal in `/issue`'s format, as the Design shows. An unsure field shows its top two values. Effort is marked `(check)`. Urgency adds `possible 5` when that flag is set. Exit 0 on success. On an API failure it exits 2 with the error on stderr.
- `commands/issue.md` step 4: run `propose` and use its line for the fields in `JEV_FIELDS`. The agent proposes the rest as today. On exit 2, say the call failed and propose every field the old way. The `Proposed ranking:` line of a unit's proposal uses the same output.

**Files:** `scripts/issue_score/score_issues.py` (`propose`), `scripts/issue_score/test_score_issues.py`, `commands/issue.md`.

**Workers:** `1 writer + 1 tester` — `impl` owns `score_issues.py` and `commands/issue.md`; `test` covers the output format, the unsure display and exit 2 on a failed fixture.

**Constraints from prior phases:** set by phase review.

**Acceptance gate:** the merge gate is green; basedpyright clean; one live `propose` on a held-out issue's title and body prints a line that matches its report row.

### Phase 5 — reassess every open issue  · status: todo

#### Work Order

**Goal:** every open issue has a Jev proposal for each field in `JEV_FIELDS`, and the user reviews current values against Jev's and decides which to write.

**Spec:**
- `score` covers all open issues: rated, default-rated (Review decisions 1), unrated, and style issues.
- Style issue state: title, project, the rule's name and its one-line summary from the `style_rule` file, `style_crate`, and the finding count.
- `score_issues.py reassess <run-id>` writes a review file, `~/.local/state/issue-score/reports/<run-id>-reassess.md`. Each issue gets one row: its current value and Jev's value for each field, sure or unsure, sorted so the largest differences come first. Unrated and default-rated issues come in their own group.
- Writing approved values goes through `proposal_issue.py stars` or a direct frontmatter edit of only the five rating fields, `category` and `area`. The watcher recomputes score and rank. The scorer itself still never writes to the vault.

**Pending decision: how the user approves reassessed values**

Actual problem:
About 640 open issues will each have up to seven Jev values. Reviewing every field of every issue one at a time is too much to read.

What exists now:
- `/prioritize` reviews one band of issues in a single proposal table and writes what the user approves.
- `/showrunner:proposal` writes proposal stars through `proposal_issue.py stars`.

What should change:
- Decide after Phase 3 shows how often Jev is sure and right for each field.

Recommendation:
Accept in bulk every value Jev is sure of for the fields the user approved in Phase 3, write those for unrated and default-rated issues first, and walk the rest by `/prioritize` band.

**Files:** `scripts/issue_score/vault.py`, `scripts/issue_score/questions.py`, `scripts/issue_score/score_issues.py` (`reassess`), `scripts/issue_score/test_reassess.py`.

**Workers:** `1 writer + 1 tester` — `impl` owns the three modules; `test` covers the style state, the grouping and the sort order.

**Constraints from prior phases:** set by phase review.

**Acceptance gate:** the merge gate is green; basedpyright clean; a full reassess run within budget; the user has the review file, and the values the user approved are written.

### Phase 6 — the TypeSafe guide for other sessions  · status: todo

#### Work Order

**Goal:** `docs/typesafe.md` lets any session use TypeSafe here without this unit.

**Spec:** in STE, under 150 lines:
- that live calls run as `with-typesafe <cmd>`, that the key is never read or printed, and the data rule (what may be sent);
- how to reuse `scripts/issue_score/client.py`;
- the `questions.py` pattern (all wording in one module, model pinned, payloads logged);
- the skill's core rules, with links to the live docs;
- the measured lessons from Phases 3 and 5 (`lessons.md`);
- the cost of a pass;
- the instruction to ask `typesafe-unit` for anything not covered.

**Files:** `docs/typesafe.md`.

**Workers:** `1 writer`.

**Constraints from prior phases:** set by phase review.

**Acceptance gate:** the doc exists, every path it names exists, and the merge gate is green.
