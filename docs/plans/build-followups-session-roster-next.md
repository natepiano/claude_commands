# Session roster — Next

## Items to consider

- [ ] **Postmaster trial: deliver send.py messages through a mod, with no model turn**
  - Step 1 shipped and its live start-up passed on natedev; the trial started at 10:07 EDT on 2026-10-13, which begins step 2's two-day window. The design and the step 2 log checks are in `docs/as-built/session-roster.md`, `### Postmaster mod (trial)`.
  - Target: `scripts/message/send.py` (`relay()` stays as the fallback), a new mod at `mods/postmaster` (`~/.claude/mods/postmaster`), `scripts/message/sessions.py`
  - Why needed: about 600 script-sent messages a day reach a Claude session, each through a `claude -p` relay (median 4.5 s of model time and about $0.029 each, measured over 192 relays). A mod in one idle session per machine can deliver from an outbox with no model request.
  - Completion condition: section 8 step 1 of `~/.local/state/plan-backlog/2026-10-11-claude-codex-mods.md`. On natedev, with no settings change: one idle `claude` session in tmux loaded with `--plugin-dir ~/.claude/mods/postmaster`; `send.py` writes each Claude-bound message to an outbox and waits up to 3 s for the mod's result, and falls back to the relay when the heartbeat is stale or no result comes. The four unverified points in that document's section 7 are checked first with two test sessions. Step 2's two-day measurement decides adoption.
  - Revealed by: the user, 2026-10-12: "Let's trial post master to see if it works"
  - When and who: shutdown-unit's next run after the per-session shutdown rewrite (`docs/as-built/shutdown-and-restart.md`). Send the showrunner the plan and its ETA when that run starts.

- [ ] **send.py names its message states instead of bare optionals**
  - Target: `scripts/message/send.py` (`Options`, `Message`)
  - Why needed: the default sender, the derived summary, unkeyed delivery, the body source and the repeat policy are bare optionals that every caller must interpret (type design contract). In `Options` these are `sender`, `summary`, `key`, `repeat_minutes`, `text` and `file`; in `Message`, `key`. Two combinations the types allow are refused only by checks at parse time: a repeat with no key, and a message to the user with no summary. Routing is already named (`RecipientResolutionScope`) and needs no change.
  - Completion condition: each is a named state converted at the CLI and storage boundary, kept flat on disk, with a test per state. Output, exit codes and queue files do not change.
  - Revealed by: Phase 2 (architect review)
  - When and who: after the shutdown rewrite (`docs/as-built/shutdown-and-restart.md`) is finished, as its own run by shutdown-unit, which owns `scripts/message/`. User, 2026-10-12: "okay have the appropriate unit do this after the shutdown rewrite." It follows the postmaster trial above, which changes `send.py` first (shutdown-unit's ordering, 2026-10-12).
