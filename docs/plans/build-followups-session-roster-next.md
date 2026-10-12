# Session roster — Next

## Items to consider

- [ ] **send.py names its message states instead of bare optionals**
  - Target: `scripts/message/send.py` (`Options`, `Message`)
  - Why needed: the default sender, the derived summary, unkeyed delivery, the body source and the repeat policy are bare optionals that every caller must interpret (type design contract). In `Options` these are `sender`, `summary`, `key`, `repeat_minutes`, `text` and `file`; in `Message`, `key`. Two combinations the types allow are refused only by checks at parse time: a repeat with no key, and a message to the user with no summary. Routing is already named (`RecipientResolutionScope`) and needs no change.
  - Completion condition: each is a named state converted at the CLI and storage boundary, kept flat on disk, with a test per state. Output, exit codes and queue files do not change.
  - Revealed by: Phase 2 (architect review)
  - When and who: after the shutdown rewrite (`docs/plans/build-followups-shutdown-per-session.md`) is finished, as its own run by shutdown-unit, which owns `scripts/message/`. User, 2026-10-12: "okay have the appropriate unit do this after the shutdown rewrite."
