# Simplify review

Report only. Edit no file. Keep these agents: $agents.
Run each Agent call in the foreground.
Wait for all listed agents to return their results before you return the final result.

Return exactly one of these forms:

1. Write `Pass` when you find no item.
2. Write a numbered list when you find items.

After either form, add an `Agent results` section.
Include each agent's name and its part of the review.
Refer to finding numbers, or write `Pass` for an agent with no finding.

Give each item a file, a line, the proposed change, and its reason.
Mark each item `in-phase` or `held-for-decision`.
An in-phase item preserves behavior and stays within the phase change.
Hold every behavior change and every change outside the phase.
A held item goes to the user only when it touches money, safety, product behavior the user sees, or an act that cannot be undone. The director settles every other held item.
Never hold an item that removes a copy. Mark it `in-phase`, also when it is outside the phase change.
The director fixes it in the unit's own files, or sends it to the owner of the other files.
Hold an item that adds a copy.
Report every item. The director records each item in the findings ledger.

Use the Three Gods: Simple, Fast and Beautiful, with correctness as the floor beneath them.
