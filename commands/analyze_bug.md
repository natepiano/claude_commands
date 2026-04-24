A bug was found. Analyze and propose a fix. Do not start implementing.

Your proposal must answer:

1. **What about the type system let this invalid state be representable?** This is the most important question. Do not skip it. The goal is a shape change that makes the bad state unrepresentable — not a conditional that probes for it at runtime.

2. **Why did existing tests not catch this?** Identify the gap.

3. **The fix**, including:
   - The test that should be written first — one that reproduces the bug and fails for the right reason.
   - The code change that makes it pass.
   - Whether this is a small local fix or a larger redesign. Match the size of the proposal to the real problem — a quick fix is fine if (1) allows it; a larger change is fine if (1) demands it.

Do not reach for `if` / `match` / null-checks as the fix until you have ruled out a type-level change. C-style defensive conditionals are the failure mode this command exists to prevent.

Wait for approval before implementing.
