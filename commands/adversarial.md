---
description: Adversarial review of the current topic — two agents take opposing sides (for and against), answer each other once, and a neutral third agent judges. Any role, any time.
---

`$ARGUMENTS` — optional clarifications: the exact question, the sides, the metric, what evidence counts, whether experiments are allowed. Empty means the topic under discussion now.

## 1. Frame

Write `<SCRATCH>/adversarial/<slug>/frame.md` (the session scratchpad, or `/tmp` when there is none):

- **Question:** one claim a side can win or lose, e.g. "running clippy inside mend's compile saves agent time on natedev".
- **Mode:** *argue* (advocates weigh evidence) or *prove* (each side must show it with a test, a measurement or a counterexample). Default argue; prove when the claim is checkable.
- **Metric:** what a verdict is stated in, fixed now so the verdicts compare: net minutes per day, pass/fail, which bug, the cost in tokens. In argue mode with no direct metric at hand, name one or more proxies (what each stands in for and how it can mislead), and have both sides argue in them.
- **Facts:** the raw material, with absolute paths: files, commits, data sources and how to query them, prior measurements and their method. Facts first; an existing conclusion goes last, as one claim to test, never as "the analysis to attack". Both sides get the same facts.
- **Stake:** if this session made the claim under review, say so. That is why a third agent judges.

## 2. Launch both sides

Two `Agent` calls in one message: `subagent_type: general-purpose`, `run_in_background: true`, named `for` and `against`. Neither sees the other's work. Both prompts share one skeleton and differ only in the side paragraph and the hunting list:

- **Side:** "Show that <claim>." / "Show that <claim> does not hold, or costs more than it gives." Then: "Another agent argues the opposite side. If, after real effort, the evidence does not support your side, say so plainly."
- **Frame:** `frame.md` verbatim.
- **Look for, among others:** a list fitted to that side: confounds, baselines, noise, comparability, the cost side, the other machine.
- **Rules:** read-only; no edits, no `git stash`, no `cd`, no credentials; read `~/rust/nate_style/rust/forbidden-words.md` before writing; report exit codes as they are. An experiment runs only when `$ARGUMENTS` allows it, in a `git clone --local` under `/tmp`, and states its cost.
- **Return,** in short plain words: (1) the verdict in the metric, with a range; (2) each point, with the exact command, query or `file:line` that shows it; (3) the strongest point against its own side that it could not knock down; (4) when the metric is a proxy, a better proxy if it found one, and why.

## 3. Rebuttal

When both have reported, `SendMessage` each the other's report: "Answer it: which points you accept, which you refute and with what evidence, and your revised verdict." One round only.

## 4. Judge

Launch a third `Agent` (`general-purpose`, named `judge`) with `frame.md`, both reports and both rebuttals. It re-runs the evidence each verdict rests on and returns: the verdict in the metric, the points that held on each side, what stays uncertain, and the one test that would settle it.

## 5. Report

Stop `for`, `against` and `judge` (`TaskStop`) once read. Give the user the judge's verdict first, then the two or three points that decided it, then what stays open and the test that would settle it. Keep the reports in the scratch folder and name its path.
