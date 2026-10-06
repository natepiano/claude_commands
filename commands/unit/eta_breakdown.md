---
description: Break this unit's phase ETA into the steps left before it, each with what it does, how long it should take and when it should end, for the user to review.
---

# ETA breakdown

Answer in this turn from what is already recorded; start no runs and do not pause the phase.

1. **Steps.** List the steps left in the current phase, in order, from now. They are the delegate's own stages: seats writing code and tests, review, repair rounds, live smoke and shots, phase review, checkpoint and the showrunner's merge, then the final gate and as-built when they come next. A step under way is listed with the time it has left.
2. **Durations.** Time each step from the recorder's durations for the same step, earlier in this run or project (`~/.claude/scripts/delegate/progress_history.py` `timeline`, `aggregate`). A step no record covers is marked `(estimate)`.
3. **End times.** Chain them from now. The last end time is the phase ETA. When it differs from the ETA last stated, the ETA moves to match the bullets; say so and send the showrunner the new ETA as `/unit:eta` words it (`commands/unit/eta.md`).
4. **Read every bullet twice before sending.** The user has not seen the plan, the board, the reviews or this session. Each bullet must tell them, in a few plain words, what is being built or checked and why that step exists, so they can judge whether the time is right. Rewrite any bullet that leans on context only this session holds, names a stage without saying what it does to this phase's work ("review" alone, "repair round 2"), or uses tooling words. Then cut every word that adds nothing. `~/.claude/docs/user_facing_explanation.md` holds the full method.

## Answer

Print it as plain Markdown, a first line and bullets, never inside a code block: the fence below only shows the shape, and monospace is hard to read (user, 2026-10-06). Send it to the showrunner when it asked:

```
<unit> phase <N>: <what the phase delivers, in plain words>. ETA <HH:MM zone>
- <what the step does to this phase's work>: <M> min, ends <HH:MM>
- …
```

Every time is in the production doc's **User zone**. No ids, seat names or batch letters.
