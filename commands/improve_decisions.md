---
description: Dialog to improve ~/.claude/docs/decision_criteria.md — one change at a time, applied succinctly.
argument-hint: [what to change, or blank to be asked]
---

# improve_decisions

Improve `~/.claude/docs/decision_criteria.md`, the single source for how to
decide what reaches the user when coding and reviewing code. `CLAUDE.md` and
`/plan:delegate` both `@`-import it, so every edit lands in every session.

`$ARGUMENTS` — the change the user wants, in their words. Blank means ask.

<Constraints>
- Keep the `<DecisionEconomy>` … `</DecisionEconomy>` tags. `/plan:delegate`
  resolves `<DecisionEconomy/>` against them at two call sites.
- One criterion per labeled bullet, stated once. No preamble repeated per bullet.
- The file loads every session: no project names, paths, or tooling mechanics.
- Shorter or equal length unless the user is adding a genuinely new criterion.
</Constraints>

<Steps>
1. Read the file. Read the two memories that point at it only if the change
   touches their subject: `feedback_decide_dont_ask.md`,
   `feedback_api_private_first.md` under
   `~/.claude/projects/-Users-natemccoy--claude/memory/`.
2. With no `$ARGUMENTS`, ask exactly one question — which single thing to
   improve — offering the concrete candidates you see: a rule that fired wrong
   recently, a missing category, ambiguous wording, a bullet to cut. Then stop.
3. Propose the edit as old text → new text for the affected bullets only, plus
   one line on what changed. Never dump the whole file.
4. Apply with Edit on approval.
5. Report the change, line count before → after, and any `CLAUDE.md` line or
   memory pointer the edit made stale.
6. Ask whether to improve one more thing. Stop when the user says done.
</Steps>

<AntiPatterns>
- No examples, rationale paragraphs, or hedges added to justify a rule. A
  criterion is one bullet: the decision, and its already-settled answer.
- No new section for a rule that fits an existing bullet.
- No copying a criterion into a memory file — memories hold provenance only.
- No silent broadening: changing when a decision reaches the user is the point
  of the file, so state that effect in one line before applying.
</AntiPatterns>
