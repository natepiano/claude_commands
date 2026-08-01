# Type design

> The user "never wants to look at a type and have to guess what it is for."

Apply this to implementation, review, fixes, escalation, and plan revisions:

- Name a type for its semantic role, state, lifetime, or guarantee. Do not name
  it only for how it was obtained, stored, or represented.
- Prefer `LastKnownGoodConfiguration` to `CapturedConfiguration` when the value
  is specifically the last configuration known to work. If the current type
  contains broader or different semantics, restructure the data and ownership
  boundaries until the precise name is true. The refactor is worth the readable
  type.
- Strongly avoid `Option<T>` in domain-owned types and APIs. If another API
  requires `Option<T>`, convert at that boundary into a domain type whose name
  and variants state what the presence or absence means. The design target is
  `SelfDocumenting<T>`: a reader should learn the domain meaning from the type
  without tracing callers or guessing.
- Treat a vague type name or bare `Option<T>` as a design finding, not cosmetic
  naming feedback. Reviewers must ask whether a more truthful type requires a
  better boundary, distinct states, or a small restructuring.
