Trace a method through the codebase before proposing any move. The goal is to find the *real* best home (which may be "delete it"), not to ratify wherever it currently lives.

**Argument:** the method to trace (e.g. `App::ci_for`, or just `ci_for` if unambiguous).

Do not propose a move, suggest a refactor, or write code. Output a report. Wait for direction.

## Steps — do all of them, in order

1. **Read the method body.** Every external call inside it. For each call, name what subsystem / module / type owns it.

2. **Find every caller.** Productive + tests + internal. `rg` the whole crate. Don't stop at the first few — count them.

3. **Read each productive call site enough to answer:**
   - What kind of value does the caller actually pass in? (Already-canonical? Pre-validated? Specific type that's narrower than the signature?)
   - Where did that value come from? (Trace one hop back if needed.)
   - Does the caller already have the other state the method's body consults? (display_mode, tracker, project_list — whatever the body reaches for.)

4. **Identify dead weight in the method body.** Does any step do useful work given what callers actually pass? Common failure modes:
   - Wrapper does path canonicalization, but every caller passes canonical paths.
   - `Option::and_then(|_| f(x))` chains where the discriminator is checked twice.
   - Existence guards that are redundant given a caller-side invariant.
   If a step is dead weight, say so.

5. **Map data ownership of every state the body touches.** For each subsystem/type the method reads from:
   - Does it own that data, or borrow access?
   - Could the body live there with the *other* inputs as parameters?
   - Is there a foundational structure (path-keyed lookups, project-tree facts) where this kind of question already lives?

6. **Question the containing module.** Is the method's current home coherent? Specifically:
   - Is the file/struct that hosts it a kitchen sink? (1000+ lines of unrelated helpers.)
   - Was the home picked because of "two subsystems are involved"? That's not a reason — name which subsystem owns the *question*.
   - If the boundary is the problem, say so. The fix may be to move the boundary, not the method.

7. **List candidate homes with one-sentence justifications each.** Include "delete and inline at N sites" as an option whenever step 4 found dead weight or N is small.

8. **State the verdict.** Pick one. Justify in two sentences.

## Anti-patterns this command exists to prevent

- **"App is the natural combiner."** This is rationalization. The question is who owns the *question*, not who currently hosts the method.
- **Stopping the trace at "I found a plausible home."** Keep going. The first plausible home is rarely the best one.
- **Defending the current location from inside the frame the method lives in.** Step outside. Ask whether the home would be invented today if it didn't exist.
- **Treating wrappers as load-bearing without checking what callers actually pass.** Many wrappers solve problems no caller has.
- **Ratifying coupling because "the helper already exists."** Helpers that bundle two unrelated computations because they share a parameter are structural coincidence, not design.

## Output sections

Brief, in order:

```
Body — what it does and what it touches
Callers — count + one-line per productive caller
Caller-side reality — what they actually pass; what's redundant
Data ownership — who owns each input/state
Boundary check — is the containing module coherent?
Candidate homes — list with justifications, including delete/inline
Verdict — which home, why, in two sentences
```

Do not produce code. Do not start the move. Stop at verdict.
