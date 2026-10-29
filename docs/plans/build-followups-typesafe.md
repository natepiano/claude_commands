# typesafe

> **Status: IMPLEMENTATION PLAN — phased, direct-ready; approved by the user 2026-10-26.** A script that has TypeSafe's Jev score each hanadocs issue field, measured against the values the vault holds now, and then used by `/issue` to propose fields for a new issue.

> **Production: natedev** — unit `typesafe-unit`; production doc `docs/plans/natedev-production.md`

## Source

2026-10-26 20:16 AST

research how to use typesafe - i want you to become a resource for other sessions on its use. your first task will be to create a script that will score each of the fields we use for issues - i want a plan for how it works first - so we can review it. also i want to use typesafe on creation of new issues so it can appropriately cateogorize fields for an issue. we'll need to come up with appropriate prompts and thresholds but for now i just want you to use the skill to research how to approach these - as well as search the web - and come up with a plan and let natedev know when the plan is ready to review

## Design — how the scorer works

### What the vault holds now

Measured 2026-10-26 on natedev's `~/rust/hanadocs`:

- 543 open issues. 96 of them are style-rule proposals (`tags: style`, `style_rule:` set). The style production writes those in bulk with fixed ratings and an empty body, so the scorer skips them.
- That leaves 447 open issues, 40 of them unrated, so about 400 rated open issues to measure against. Closed issues are never scored (Review decisions 3).
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

- **Labels:** the ratings of about 400 rated, open, non-style issues. A hash of each file name splits them, the same way on every run, into a 70% tuning set and a 30% held-out set. The held-out set is scored once, after tuning ends.
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
3. A human baseline. Answered in Review decisions 4: a blind preference test.

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
   - The user, after this answer: "once we get jef working - we should apply it to the defaulted and missing - and in fact i want to reassess all of our issues in case i like jev's categorization better". So the plan adds a reassessment phase (Phase 6) that scores every open issue, including style issues, issues with default ratings and unrated issues. Style issues need their own state, built from their rule fields because their body is empty.
2. **Budget: $20.** The user: "i am happy to add $20 to jev". The scorer keeps a running total from each response's `usage.input_tokens` at $0.042 per million. It stops before a request that would pass $20, and asks the user before it spends more.
3. **Open issues only.** The user: "we only need to score open issues - not closed". `score`, `compare` and `reassess` read open issues only, and closed issues are not labels either.
4. **Baseline: blind preference.** The user: "blind preference for our teating". In Phase 3, about 20 held-out issues where Jev and the vault disagree are shown to the user. Each issue shows its two values as A and B, in random order, with no label saying which is Jev's. The user picks A, B or same. The share of picks for Jev decides how far the reassessment trusts Jev. This replaces the blind re-rating.
5. **Plan approved.** The user: "approve" (2026-10-26). `/unit:direct` starts at Phase 1.

### Resource for other sessions

`typesafe-unit` stays on to answer other sessions' TypeSafe questions. Phase 7 writes `docs/typesafe.md` so the knowledge outlasts this session: how to run a command under `with-typesafe`, how to reuse the client, the `questions.py` pattern, and the measured lessons from Phase 3. Those lessons cover which wordings won, the order-bias results, threshold values, and where Jev was wrong.

## Delegation Context

- **Project:** `~/.claude` — the user's Claude Code configuration; this unit adds `scripts/issue_score/`, a TypeSafe-backed scorer for hanadocs issue fields.
- **Project started:** 2026-10-27T00:44:08.723+00:00
- **Stack:** Python ≥ 3.11, standard library only, run through `~/.claude/scripts/lib/py`; TypeSafe HTTP API `POST https://api.typesafe.ai/v1/systemone`, model `jev-1.14.0`; schemas at `https://api.typesafe.ai/openapi.json` and `https://docs.typesafe.ai/api/system-one.md`.
- **Layout:**
  - `scripts/issue_score/` — new: `client.py`, `vault.py`, `questions.py`, `decide.py`, `evaluate.py`, `score_issues.py` (CLI), `test_*.py`, `fixtures/`
  - `commands/issue.md` — `/issue`; step 4 gains the `propose` call (Phase 5)
  - `docs/typesafe.md` — new guide (Phase 7)
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

### Phase 1 — client, state and questions; the `score` command  · status: done

#### As-built

- `score_issues.py score [--only glob] [--limit N]` scores open non-style issues, 8 workers at a time. Each issue gets a main request and an area request. The run writes one JSONL line per issue to `~/.local/state/issue-score/runs/<run-id>.jsonl` and prints that path.
- A result line holds `issue_path`, `content_hash`, `question_set_version`, `model`, both `payloads`, all `answers` and `decided_fields`. A failed issue writes `{"issue_path","error"}`. `score()` returns `ScoreRun(result_path, failure_count)`; `main` exits 1 on any failure.
- The cache reuses a line when (path, content hash), `QUESTION_SET_VERSION` and model all match. The hash covers title, project, body and the area vocabulary, never labels. A partial last line from a killed run is ignored.
- `BudgetExceeded` stops the run: no new batch starts, and finished lines stay.

**Files:**
- `scripts/issue_score/client.py` — `system_one`, response `TypedDict`s (`NoulAnswer`, `ChoiceAnswer`, `ScoreAnswer`, `Usage`, `Response`), `TypeSafeError`, `BudgetExceeded`, spend ledger and reservations
- `scripts/issue_score/questions.py` — all question wording, `PROJECT_ABOUT`, constants, `main_questions()`, `area_questions(areas)`
- `scripts/issue_score/decide.py` — median, split, two-order Choice average, urgency table, `decide_answers`
- `scripts/issue_score/vault.py` — `Issue`, issue loaders, `clean_body`, `normalize_area`, `area_vocabulary`, `build_state`
- `scripts/issue_score/score_issues.py` — the command line and `score` subcommand
- `scripts/issue_score/test_*.py`, `fixtures/*.json` — client, questions, decide, vault and command tests on recorded responses
- `pyrightconfig.json` — an entry for `scripts/issue_score`
- `scripts/production/merge_gate.py` — `issue_score` in `TEST_SUITES` (`test_merge_gate` `EXPECTED_SUITES` matches)

**Binds later work:**
- `client.system_one(state, questions, model=MODEL, *, ledger_path=SPEND_LEDGER) -> Response`. The key comes only from `TYPESAFE_API_KEY`; missing gives `TypeSafeError("TYPESAFE_API_KEY not set: run under with-typesafe")`. It retries 429/529 at most 5 times. The budget is the `spend.jsonl` ledger plus byte-bounded reservations in `spend.pending.json` under an fcntl lock. A reservation persists after a killed process (fail closed). Over budget raises `BudgetExceeded`. `score` puts the ledger at `<state_dir>/spend.jsonl`.
- `questions.py` holds all wording, `QUESTION_SET_VERSION` (`"2"`), `MODEL` (`"jev-1.14.0"`), `BUDGET_USD = 20.0`, `THRESHOLDS` (0.6 each), `EVIDENCE_PRESENT` 0.7 / `EVIDENCE_ABSENT` 0.3, `SPLIT_PROBABILITY` 0.25, `BODY_CHARACTER_LIMIT` (24,000). Production question names are fixed: `goal`/`goal_reverse`, `category`/`category_reverse`, `impact`, `effort`, `pressure`, `alignment_<goal>`, the 4 urgency evidence Nouls (`deadline_named`, `outside_cutoff`, `current_harm`, `blocks_planned_work`), and `area`/`area_reverse`/`area_fits`.
- `decide.decide_answers(main, area) -> dict[str, FieldDecision]`; `FieldDecision(value, confidence, certainty, split, check, possible_five, probabilities, hint)`. Urgency evidence > 0.7 gives 4 with `possible_five` and probabilities `{"4": confidence}`; urgency never returns 5. `area_fits` < 0.3 gives `unfiled`, keeps the top area as `hint`, and takes certainty from 1 − fits.
- `vault.load_open_issues()` returns open non-style issues. `vault.load_rated_issues() -> RatedIssues(issues, skipped)` gives each skip a reason (not open, unconfirmed Proposed ranking, missing or invalid ratings, style ratings match proposal formula). `vault.build_state(issue)` raises on an unknown (non-canonical) project. Labels are never in the state.

**Gotchas:**
- `EFFORT_LEVELS` examples name open issues (enum variant index, NonZeroSize) that are in the evaluation set.
- A version-titled issue (for example "0.20.0 …") fires `deadline_named`, so it gets urgency 4 with `possible_five`.
- Category leans feature/research where the vault says enhancement.
- 9 style issues pass as labels, because their values differ from the style formula on current counts.
- A live run of 5 issues cost about $0.0019; the whole vault costs about $0.17.

### Phase 2 — the `compare` report  · status: done

#### As-built

- `score_issues.py compare <run-id> [--split tune|holdout|all] [--repeat <run-id>]` calls `evaluate.compare(run_id, *, split="all", repeat_run_id="", issues_dir, state_dir, goals_path) -> Path`. It reads only saved run lines and current vault labels, never the API, and writes `~/.local/state/issue-score/reports/<run-id>-<split>.md`. It refuses a state dir inside the vault.
- `evaluate.split_for(filename)` gives tune when `sha1(filename) % 10 < 7`, else holdout; `in_split(filename, split)` applies it. `score` gains `--split` (default `all`), so a tune run sends no holdout issue, and `--fresh`, which skips the cache read but still writes results.
- The header counts scored, failed and unscored eligible issues; the title says "partial" when any issue failed or is unscored. Labels and `backlog_score` come from `vault.load_rated_issues()` at compare time.
- Groups: all, title-only (cleaned body under 200 characters), and `backlog_score` ≥ 20. Ordinal fields get exact, within one, MAE, quadratic-weighted kappa, RPS normalized by K−1, and a confusion matrix. Choice fields (`backlog_goal`, `category`, `area`) get accuracy, confusion, Brier (sum over classes) and reliability in 5 confidence buckets. Each group gets the Spearman correlation between `renumber.calculate_score` on the Jev stars and the current `backlog_score`. A metric with no defined value prints its `UndefinedMetric` reason.
- After the groups: a risk-coverage table per field at thresholds 0.40–0.95, the 20 largest disagreements per field, and with `--repeat` a per-field change rate over issues whose hash, question version and model match.
- Probability metrics read the question answers in `answers`, never `decided_fields.probabilities`. Urgency RPS uses the `pressure` Score. Urgency 4 from evidence and a derived `unfiled` area get exact match only (`ExactMatchOnly`). Alignment reads `alignment_<goal>` for the decided goal, at the lower of its own and the goal confidence. Split answers, two-order disagreement and urgency evidence between 0.3 and 0.7 stay unsure at every threshold.
- Baseline run `20261027T085930.573291Z-970cbc23` (tune, 330 issues, 0 failures, $0.12; 317 scored after 9 effort examples and 6 style issues are excluded): goal 96%; urgency 77% exact but kappa 0.08; category 68%; impact 54% (within one 96%); effort 52% (91%); alignment 51% (92%); area 47%; composite Spearman 0.55.

**Files:**
- `scripts/issue_score/evaluate.py` — `Split`, `split_for`, `in_split`, the metric functions (`confusion_matrix`, `quadratic_weighted_kappa`, `ranked_probability_score`, `brier_score`, `spearman`), `FieldObservation`, `ExactMatchOnly`, `UndefinedMetric`, `compare`
- `scripts/issue_score/records.py` — the run line shapes (`RequestPayload`, `ScoredIssue`, `FailedIssue`, `ScoreRun`) and the one `STATE_DIR`; `score_issues.py` and `evaluate.py` import them
- `scripts/issue_score/score_issues.py` — the `compare` subcommand; `score --split` and `--fresh`
- `scripts/issue_score/questions.py` — `EFFORT_EXAMPLE_STEMS`
- `scripts/issue_score/vault.py` — `Issue.is_style`; `LABEL_FIELDS` includes `backlog_score`
- `scripts/issue_score/test_evaluate.py`, `test_score_issues.py`, `test_questions.py` — hand-computed kappa, RPS and Spearman cases; `--split` and `--fresh` cases

**Binds later work:**
- `questions.EFFORT_EXAMPLE_STEMS` is a frozenset of 10 exact open vault filename stems. `compare` excludes these by `issue.path.stem`, and also excludes admitted style issues, which it lists for review. The baseline run had these issues in its prompt, so they stay excluded from every comparison with it, also after the `EFFORT_LEVELS` examples change.
- `unfiled` has two meanings. It is a real vault area (4 issues) and so an ordinary Choice option. It is also the derived fallback when `area_fits` < `EVIDENCE_ABSENT`. Only the derived one is exact-match only.
- The baseline to beat is run `20261027T085930.573291Z-970cbc23`. Area and category are the weakest fields. The 1-star majority inflates urgency exact match; judge urgency by kappa and the 4–5 rows.

**Gotchas:**
- `renumber.py` imports `writer_lock` by bare name, so `evaluate.py` inserts `scripts/prioritize` into `sys.path` once (guarded) before it imports `renumber`.

**Ruled out:**
- Effort-example exclusion by title text: substring matches select the wrong issues; exact filename stems replace it.

### Phase 3 — tune wordings and thresholds; the user's review  · status: done

#### As-built

- `questions.py` holds the tuned wordings at `QUESTION_SET_VERSION` "3" (adopted wordings in `fixtures/adopted_wordings_v3.json`). `THRESHOLDS` maps each field to a named outcome, never a nullable number: `Supported(threshold)`, `Unsupported` (no threshold meets the error target, or sure coverage is zero; the field is agent-only) or `ReportOnly`. Goal, impact and alignment are `Supported(0.40)`; category is `Supported(0.90)`; area is `Unsupported`; urgency and effort are `ReportOnly`.
- `JEV_FIELDS` = `backlog_goal`, `backlog_impact`, `backlog_alignment`: the user's verdict after the blind preference test, 2026-10-29.
- One sure predicate in `decide.py`; `evaluate.py` risk coverage calls it, sweeps one field's threshold and holds every other field at its `THRESHOLDS` value. Split answers and Choice orders that disagree stay unsure at every threshold.
- `compare <candidate> --baseline <run-id>` judges both runs on one cohort: the issues eligible in both, the same labels read once, `HISTORICAL_EFFORT_EXAMPLE_STEMS` excluded from both. That set is never empty.
- `experiment.py` asks wording variants as `<name>__<variant>` questions in one request over one state, and maps each back to its production name before `decide`. A variant file replaces whole questions by production name, and a variant must keep the production option set.
- `score_issues.py blind <run-id> --count 20` writes an A/B sheet of held-out disagreements across fields, shuffled by a recorded seed, with the key in a separate file. `blind-score <sheet>` reports the Jev, vault and same shares per field.
- Held-out result: goal, impact and alignment pass; category at 0.90 fails (error .15). Blind result: Jev 12, vault 8.

**Files:**
- `scripts/issue_score/questions.py` — wordings, `THRESHOLDS`, `JEV_FIELDS`, `QUESTION_SET_VERSION`, `HISTORICAL_EFFORT_EXAMPLE_STEMS`
- `scripts/issue_score/decide.py` — threshold outcome type and the shared sure predicate
- `scripts/issue_score/evaluate.py` — risk coverage through the shared predicate; one cohort
- `scripts/issue_score/experiment.py` — variant questions in one request, mapped back to production names
- `scripts/issue_score/blind.py` — blind sheet, key and tally
- `scripts/issue_score/records.py` — variant runs and the blind key
- `scripts/issue_score/score_issues.py` — `blind`, `blind-score`, `compare --baseline`, `--variants`

**Binds later work:** **`/issue` proposes with Jev** and **reassess every open issue** read `JEV_FIELDS`; `test_questions.py` pins it, and it grows only on the user's verdict. **area and category definitions; route unsure answers to the user** re-tunes the category and area thresholds.

**Gotchas:**
- The derived `unfiled` fallback (`area_fits < EVIDENCE_ABSENT`) and a Choice pick of the real area `unfiled` differ by the decision's origin, never by the value string.
- A cache hit recomputes decisions under the current policy.
- Area options carry no definitions, and each project has its own area names, so a variant file cannot express a defined-area variant; that needs code.
- Feature, enhancement and research are the main category confusion.

**Ruled out:** Jev proposes category at 0.90 — held-out error .15.

### Phase 4 — area and category definitions; route unsure answers to the user  · status: todo

**Depends on:** Phase 3

#### Work Order

**Goal:** category and area answers that are at most 10% wrong where Jev is sure, with every unsure answer routed to the user, measured on the tune split and checked once on held-out.

**Spec:**
- Area definitions: one line per area of the global vocabulary, drafted by the unit director from the area's open issues. They live in `~/rust/hanadocs/conf/issue-areas.md` (outside `issues/`, which the prioritize and dailies scripts scan), a table `area | definition | issues`; this unit owns that one vault path and commits only it to hanadocs main (pull --rebase first, never `-a`). The lines are marked as drafts the user can correct. `vault.py` reads the file read-only; an area with no line keeps today's option text.
- `area_questions` takes the definitions and uses each one as that area's option criteria. The definitions go in the cache key, with a test that a changed line misses the cache.
- Experiment: the defined-area options keep the production option keys and change only their text, so the existing variant file expresses them, beside the category variants; no new experiment code. The unit director runs the tune-split experiment before dispatch, and the writer applies only adopted wording.
- The unit director sharpens the feature, enhancement and research definitions in `CATEGORIES`, and runs both variants on the tune split through `compare --baseline`. A variant is adopted only when it gains on the tune split; each adoption increments `QUESTION_SET_VERSION`.
- Routing (the user's rule, 2026-10-29): per field, the threshold is the lowest top-answer probability whose taken answers are at most 10% wrong on the tune split. The risk-coverage report names, per field, the share taken and the share routed to the user. A field with no such threshold stays `Unsupported` and agent-proposed.
- Score held-out once after tuning. Send the category and area held-out results to the showrunner. The user reviews the drafted definitions only for a field that passes; `JEV_FIELDS` changes only on the user's verdict.
- Record the result in `~/.local/state/issue-score/lessons.md`.

**Files:**
- `scripts/issue_score/vault.py` — read the area definitions file
- `scripts/issue_score/questions.py` — defined area options; sharper category criteria; thresholds; version
- `scripts/issue_score/evaluate.py` — share taken and share routed in the risk-coverage report
- `scripts/issue_score/score_issues.py` — pass the definitions to the area request
- `scripts/issue_score/test_vault.py` — definitions file tests
- `scripts/issue_score/test_questions.py` — option text, fallback and version tests
- `scripts/issue_score/test_evaluate.py` — taken and routed shares
- `scripts/issue_score/test_score_issues.py` — a changed definition misses the cache
- `scripts/issue_score/test_decide.py` — configured threshold policy after adoption

**Workers:** `1 writer + 1 tester` — the unit director drafts the definitions and runs the experiments; `impl` owns `vault.py`, `questions.py`, `evaluate.py` and `score_issues.py`; `test` owns the five test files, and updates the measured-wording fixture only for an adopted change.

**Constraints from prior phases:**
- `JEV_FIELDS` is goal, impact and alignment (the user's verdict after the blind test, 2026-10-29). Phase 3 held-out: category at 0.90 failed (error .15); area was `Unsupported`.
- The area vocabulary is global: `vault.area_vocabulary` builds one sorted list from all open non-style issues, and `experiment.build_questions` requires a variant to keep the production option set. Definitions change only option text; a per-project vocabulary is out of scope.
- Keep the Phase 3 rules: one cohort through `compare --baseline`, one sure rule shared by `decide.py` and `evaluate.py`, the threshold outcome type (`Supported`, `Unsupported`, `ReportOnly`), a cache hit recomputed under the current policy, the repeat check with `--fresh`, and the derived `unfiled` fallback told apart from a Choice pick of `unfiled` by origin.
- Tune-split experiment (2026-10-29, 317 issues, same cohort as production): area with definitions as option text raised accuracy from .51 to .67, and 0.90 is the lowest threshold with taken answers at most 10% wrong (error .083; 38% taken, 62% routed to the user). Adopt it: `area` becomes `Supported(0.90)` and `QUESTION_SET_VERSION` becomes "4". The broad-instruction variant met the target only at exactly .100 and is not adopted. Sharper feature, enhancement and research definitions lowered category accuracy (.74 to .71) and are not adopted; `CATEGORIES` and the category threshold stay as they are, because category's held-out set was already spent at 0.90.
- No prompt gymnastics: a definition describes the area from its issues, never names a test issue, and gains no clause per failure.
- Live calls run as `with-typesafe <cmd>`; the key is never read or printed. Total live spend so far is about $0.40 of the $20 budget.

**Acceptance gate:** the merge gate is green; basedpyright clean; the tune-split comparison and the held-out result for category and area are reported to the showrunner; the thresholds and the version in `questions.py` match what was adopted.

### Phase 5 — `/issue` proposes with Jev  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** a new issue's proposal line comes from Jev for every field in `JEV_FIELDS`, and the user still confirms in one reply.

**Spec:**
- `score_issues.py propose --project <short> --title <t> [--body-file <f>]` prints the one-line proposal in `/issue`'s format, as the Design shows. An unsure field shows its top two values. Effort is marked `(check)`. Urgency adds `possible 5` when that flag is set. Exit 0 on success. On an API failure it exits 2 with the error on stderr.
- `commands/issue.md` step 4: run `propose` and use its line for the fields in `JEV_FIELDS`. The agent proposes the rest as today. On exit 2, say the call failed and propose every field the old way. The `Proposed ranking:` line of a unit's proposal uses the same output.

**Files:**
- `scripts/issue_score/score_issues.py` — the `propose` subcommand
- `scripts/issue_score/test_score_issues.py` — propose tests
- `commands/issue.md` — step 4 uses `propose`

**Workers:** `1 writer + 1 tester` — `impl` owns `score_issues.py` and `commands/issue.md`; `test` covers the output format, the unsure display and exit 2 on a failed fixture.

**Constraints from prior phases:**
- `JEV_FIELDS` is goal, impact and alignment (the user's verdict, 2026-10-29). Category and area join it only if Phase 4 meets their target and the user approves; `propose` reads `JEV_FIELDS`, never a fixed list.
- An unsure Jev field is routed to the user: the line shows its top two values and the user picks in the same reply (the user's routing rule, 2026-10-29).
- `propose --project <short>` converts the shorthand to the canonical project name (`hana_diegetic`) before `vault.build_state`, which raises on an unknown project; a test covers each shorthand and an unknown one.
- Show the derived flags: `split`, `check` (effort), `possible_five` (urgency 4) and the derived `unfiled` fallback with its hint. A Choice pick of the real area `unfiled` is an ordinary value with no hint; tell them apart by the decision's origin, and a test covers both.
- Alignment follows the final goal. The request asks `alignment_<goal>` for every goal, and `decide_answers` keeps only the winning goal's; `propose` keeps every goal's alignment answer. When goal is unsure, the line shows both goal candidates, each with its own alignment value, so the user's pick needs no second call. A named goal state (resolved goal, or unresolved first and second candidates) carries this, never an optional. A fixture with an unsure goal whose second candidate has a different alignment proves it.
- Acceptance: the output for a fixture response is equal to a pinned expected text; the live check confirms only that the output is complete and usable, because a fresh response can differ from an earlier report row.

**Acceptance gate:** the merge gate is green; basedpyright clean; the fixture output equals its pinned text; one live `propose` on a held-out issue's title and body prints a complete, usable line.

### Phase 6 — reassess every open issue  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** every open issue has a Jev proposal for each field in `JEV_FIELDS`, and the user reviews current values against Jev's and decides which to write.

**Spec:**
- `score` covers all open issues: rated, default-rated (Review decisions 1), unrated, and style issues.
- Style issue state: title, project, each rule's name and its one-line summary (from `style_rule`, or each entry of `style_rules`), `style_crate`, and the finding count. A rule file that is missing gives a named outcome in the review file, never a silent empty summary.
- `score_issues.py reassess <run-id>` writes a review file, `~/.local/state/issue-score/reports/<run-id>-reassess.md`. Each issue gets one row: its current value and Jev's value for each field, sure or unsure, sorted so the largest differences come first. Unrated and default-rated issues come in their own group.
- Approval follows the user's routing rule (2026-10-29): decisions are rebuilt from the saved raw answers under the current policy. A `JEV_FIELDS` value for which `decide.is_sure()` holds is taken, unrated and default-rated issues first; every other value goes to the user, walked by `/prioritize` band. The review file shows the two groups apart, and shows the score >= 20 group's results on their own.
- Unrated and default-rated issues: the agent proposes the missing agent-owned fields (category, area, urgency, effort) in the same review, as `/issue` does, so each issue gets a score and a band before band routing.
- `apply_labels.py` writes approved values: a frontmatter edit of only the approved fields among the five rating fields, `category` and `area`. It refuses when the issue's content hash differs from the scored one, or when an approved field's current value differs from the value the review showed (`content_hash()` excludes labels). The watcher recomputes score and rank. The scorer itself never writes to the vault.

**Files:**
- `scripts/issue_score/vault.py` — style issue state from rule fields
- `scripts/issue_score/questions.py` — any question changes the reassess needs
- `scripts/issue_score/score_issues.py` — the `reassess` subcommand
- `scripts/issue_score/records.py` — persisted style inputs in run records
- `scripts/issue_score/apply_labels.py` — the approved-field writer
- `scripts/issue_score/test_reassess.py` — reassess tests
- `scripts/issue_score/test_apply_labels.py` — writer tests

**Workers:** `1 writer + 1 tester` — `impl` owns `vault.py`, `questions.py`, `score_issues.py`, `records.py` and `apply_labels.py`; `test` owns `test_reassess.py` and `test_apply_labels.py` (style state, grouping, sort order; only approved fields change; a stale hash or a changed approved value refuses; threshold equality, structural uncertainty and stale saved certainty).

**Constraints from prior phases:**
- Read open, non-style issues through `vault.load_open_issues`; style issues get the formula state that this phase adds. `score` already covers rated, default-rated and unrated open non-style issues; the new scoring work is style inclusion only.
- The cache key holds title, project, body, the area vocabulary and (from Phase 4) the area definitions. Add every new style input (rule summaries, `style_crate`, finding count) to it, with a test per input that a change misses the cache and that the definition-sensitive key is kept.
- The derived `unfiled` fallback and a Choice pick of the real area `unfiled` show as different outcomes in the review file; a test covers both.
- Acceptance proves that only approved values change and that the scorer code stays read-only.
- A partial run (failed lines or a budget stop) shows the failed and unscored issues in the review files, kept apart from each other, and never presents them as complete; a test proves it.
- `proposal_issue.py stars` takes all five ranking values, not a subset, which is why this phase has its own writer.

**Acceptance gate:** the merge gate is green; basedpyright clean; a full reassess run within budget; the user has the review file, and the values the user approved are written.

### Phase 7 — the TypeSafe guide for other sessions  · status: todo

**Depends on:** Phase 6

#### Work Order

**Goal:** `docs/typesafe.md` lets any session use TypeSafe here without this unit.

**Spec:** in STE, under 150 lines:
- that live calls run as `with-typesafe <cmd>`, that the key is never read or printed, and the data rule (what may be sent);
- how to reuse `scripts/issue_score/client.py`;
- the `questions.py` pattern (all wording in one module, model pinned, payloads logged);
- the skill's core rules, with links to the live docs;
- the measured lessons from Phases 3, 4 and 6 (`lessons.md`);
- the cost of a pass;
- the instruction to ask `typesafe-unit` for anything not covered.

**Files:**
- `docs/typesafe.md` — the TypeSafe guide for other sessions

**Workers:** `1 writer`.

**Constraints from prior phases:**
- Explain partial runs: a failed line holds `{"issue_path","error"}`, and `score` exits 1.
- Explain spend: `spend.jsonl` holds recorded spend; `spend.pending.json` holds reservations, which stay after a killed process (fail closed). `BudgetExceeded` can come while recorded spend is below $20; give the steps to tell reserved from recorded spend and to clear a stale reservation. Acceptance checks that an interrupted-run scenario gives the operator these steps.
- Name the shared modules: `records.py` (run shapes, `STATE_DIR`), `questions.py`, `decide.py`, `evaluate.py`; explain that a complete scoring run and a complete comparison cohort are different checks (`compare --baseline`).

**Acceptance gate:** the doc exists, every path it names exists, and the merge gate is green.
