# Simplify review lens

## What it is

The `simplify` lens reviews the complete phase diff during the first broad review.
It uses four Claude agents: `reuse`, `simplification`, `efficiency`, and `altitude`.
The agents report proposed changes without editing files.
The director records every finding and sends repairs to Codex workers.

Broad review also runs `adversary` and `contract`, plus `rerun` when armed.
The `contract` reviewer owns style review.
[Dual review](../direct/dual_review.md) defines the review steps.

## How it works

### Files and launch

| File | Role |
| --- | --- |
| `docs/direct/dual_review.md` | Defines the lens and its launch during `<DualReview/>` step 3. |
| `commands/unit/direct.md` | Defines synthesis, repair dispatch, and the findings commands. |
| `config/direct.conf` | Sets `SIMPLIFY_LENS=on` and the four names in `SIMPLIFY_AGENTS`. |
| `config/models.conf` | Selects `direct.simplify=claude` and `simplify=opus:high`. |
| `config/review/simplify.md` | Supplies the standing review instructions and report format. |
| `scripts/direct/assign.py` | Checks the switch and replaces its process with `review.sh`. |
| `scripts/direct/review.sh` | Owns the launch lock, review process, files, and completion records. |
| `scripts/direct/simplify_result.py` | Prepares the prompt, checks the result, and copies the final report. |
| `scripts/agents/agent_exec.sh` | Starts Claude in plan mode with the three editing tools removed. |
| `scripts/direct/findings.py` | Stores findings, held items, answers, and closure verdicts. |
| `commands/showrunner/produce.md` | Sends the required held items to the user. |

The director calls `assign.sh --role review --lens simplify --pass 1` with the run, phase, worktree, prompt, and `--diff` path.
The diff must contain the complete phase change.
`assign.py` reads `SIMPLIFY_LENS` at launch.
It skips the lens when the switch is `off` or the pass is later than 1.
It refuses early-review and style-range options for this lens.

`review.sh` acquires `${PHASE_DIR}/review_launch_simplify.lock` before it clears earlier results.
The lock owner alone writes the report, status, and summary.
It holds the lock until the review child exits.
A second launch reports that the lens is busy and preserves those files.

The generated prompt starts with `/simplify <absolute saved diff path>`.
It combines the standing instructions with the supplied phase prompt, including the user's words.
Every launch uses a fresh Claude session, including a retry after failure.
It uses no persistent Codex reviewer address.

The lens runs on the Claude account of the unit director that starts it, through the caller's `CLAUDE_CONFIG_DIR`.
`agent_exec.sh` uses `--print --permission-mode plan --disallowedTools Edit,Write,NotebookEdit`.
It keeps `Agent` for the four review agents.
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` disables background tasks.
The prompt requires foreground Agent calls and all four results before the final report.

### Results and records

The frozen `ReviewFiles` dataclass has four `Path` fields: `findings`, `status`, `log`, and `summary`.
The standard files live under `${PHASE_DIR}`:

| Path | Contents |
| --- | --- |
| `review_prompt_1_simplify.md` | The generated prompt. |
| `review_findings_1_simplify.txt` | `Pass` or numbered findings, with each agent's result. |
| `review_agent_1_simplify.log` | The Claude event stream and launch details. |
| `review_status_simplify` | One atomic line: `preparing`, `reviewing`, `reviewed`, or `error`. |

The unnumbered findings and log names link to the current pass files.
An explicit `--summary` receives the final report; otherwise the findings file serves as the summary.
The launcher checks the worktree for changes before it accepts the review.

The result checker accepts a heading or preamble before `Pass` or item 1.
It rejects empty reports, Claude errors, and reports without that result form.
When Claude supplies agent statistics, all configured agents must complete without failure.
The prompt requires every agent's result even when statistics are absent.

The launcher records the fresh session as role `review`, lens `simplify`, in `agents.jsonl`.
It also writes a durable review record with the phase instance, times, status, and raised count.
[Review regime records](review-regime-trial.md) define the separate simplify counts and minutes.

### Findings and decisions

`<Synthesize/>` combines duplicate findings and retains every contributing lens.
Each simplify finding has severity `minor` and includes the proposed change, files, and reason.
Behavior-preserving changes within the phase enter the normal repair batch.
Changes that remove a copy enter that batch even outside the phase.
Changes that add a copy require a decision.
Other behavior changes and changes outside the phase use `--held-for-decision`.

`findings.py` stores a held item with state `held_for_decision`.
Held items remain outside repair batches and do not block the phase gate.
They remain in the ledger after the phase ends.
The checkpoint notice lists their ids, files, and proposed changes.

Only these held items reach the user:

- Changes that affect money.
- Changes that affect safety.
- Changes that affect product behavior the user sees.
- Acts that cannot be undone.

The director settles every other held item under `DecisionEconomy` and states the call in one line.
The showrunner files user questions for the listed categories.
Approved work returns to the owning unit as a `next` item or a new phase.

`decision --id F001 --answer approved|declined --user-words "<answer>"` records the user's own words.
It changes the finding state to `decided` and applies no code change.
A changed ruling requires `--withdraw "<reason>"`.
The old `decision` enters `decision_history` with `withdrawn_epoch` and `withdraw_reason`.
The new `finding_decision` event includes that old object as `withdrew`.
Repeating the same answer and words preserves the saved decision.
The command can record an answer after the phase closes.

An approved decision permits a normal repair verdict after the repair exists.
Approval alone does not accept the repair.
A held item refuses closure verdicts.
A declined decision closes the item and refuses closure verdicts.

## Invariants

- Keep all four review agents and the `Agent` tool.
- Keep plan mode and remove `Edit`, `Write`, and `NotebookEdit`.
- Run the lens only on the completed diff during the first broad review.
- Record every finding; discard none.
- Keep held items until a decision resolves them.
- Send only the four listed decision categories to the user.
- Use only the user's own words in `--user-words`.
- Preserve withdrawn decisions and require repair evidence after approval.
- Let Codex fix workers apply simplify repairs.
- Let only the launch lock owner change shared result files.

## Calibration / gotchas

A missing account, Claude failure, empty result, or invalid result makes the review unavailable.
The director names the missing lens and continues synthesis with the available reviews.
A busy launch exits 0 after its stderr report, so its exit status does not prove a completed review.
Read the status and summary.

`SIMPLIFY_AGENTS` supplies both prompt names and the expected completed-agent count.
A model or Claude output change can affect result extraction.
Keep the report contract and the agent completion check aligned with the actual output.

If the supplied prompt path equals the generated prompt path, preparation uses a `.generated.md` path.
This preserves the source prompt.
A new launch clears earlier results only after it acquires the lock.
A held finding requires the `simplify` lens; the held state is not a general exception for other lenses.

## Why

The four agents examine reuse, complexity, cost, and the level of the design before the checkpoint.
The read-only launch separates review from repair.
The existing repair batch handles accepted changes without another checkpoint cycle.
The ledger preserves proposed behavior changes while the phase continues.
The decision rules reserve the user's time for the choices that require the user.
