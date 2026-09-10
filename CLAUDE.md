## communication

### word list
- The forbidden-words list lives at `~/rust/nate_style/rust/forbidden-words.md`. It is enforced via `/rust_style` and `/style_eval` (loaded with the style guide), not at session start. Don't use those words in code, comments, or prose.

## decision criteria
Applies to every session when coding and reviewing code. `/plan:delegate` imports the same file, where it defines `<DecisionEconomy/>`.

@~/.claude/docs/decision_criteria.md

## python
- basedpyright (zed's LSP) must report zero errors and zero warnings
- **NEVER** use file-level type ignores (e.g. `# pyright: reportAny=false` at top of file)
- Avoid `Any`: annotate all signatures; use `TypedDict` for dicts with known keys; for stdlib `Any` returns (`json.loads()` etc.), annotate with a `TypedDict`/specific type. Last resort only: line-level `# pyright: ignore[reportAny]` on the specific line. Reference: `~/.claude/scripts/bevy_dependency_check.py`
- **ALWAYS** `uv pip install`, never bare `pip install`

## running long commands (builds, tests, pushes)
- **ALWAYS background long-running commands** with `run_in_background: true`, then **end my turn** — do nothing, or do other unrelated work. The harness sends a `<task-notification>` when the command finishes and re-invokes me with the result. That notification IS the wait mechanism. Don't replace it.
- **NEVER poll a backgrounded command.** No `sleep`/`grep`/`tail` loop, no `for i in $(seq …); do sleep 5; done`, no repeated reads of the output file to detect completion. If I find myself writing a wait loop, that's the bug — delete it and just wait for the notification.
- **Do NOT foreground-block a long command** (a plain Bash call with a big `timeout`) as the way to "wait" for it. Background it and yield the turn instead. Foreground is for fast commands (seconds) whose output I need inline to proceed.
- After the `<task-notification>` arrives, read the output file once to get the result. Capturing exit code: pipelines mask it — `cargo … | tail` reports `tail`'s exit (always 0), so a real failure looks like success. Use `set -o pipefail`, or don't pipe the command whose exit code matters. This is not only about long commands — it holds for **any** command whose exit status I intend to read, a one-off diagnostic probe included.
- **Never `${PIPESTATUS[0]}`.** That is bash's spelling, and both machines default to zsh, where the array is `pipestatus` — lowercase, and **1-indexed**. `${PIPESTATUS[0]}` expands to the empty string with no error and no warning, so `[ "${PIPESTATUS[0]}" -ne 0 ]` gets an empty operand instead of a status, and correcting only the case still reads element 0 of a 1-indexed array. `set -o pipefail` is the remedy to keep; reach for `${pipestatus[1]}` only when one specific element of a pipeline is genuinely what I want. Measured 2026-09-09, when the form this bullet used to recommend returned an empty answer while checking a `git fetch` — the exact failure the rule above exists to prevent, produced by the rule that teaches it.

## bevy BRP MCP
- when the user says "launch", just launch the app directly — don't try to shut down first. The user has already shut it down.
- when the user says "relaunch", shut down the app first, then launch it.

## git
- **`git stash` is denied outright and no flag gets past it.** `permissions.deny` carries `Bash(git stash)`, `Bash(git stash *)` and `Bash(git stash:*)`. **The `allow` entries for `git stash list` do not make the read form runnable** — `Bash(git stash *)` matches it too, and deny wins. Do not soften this into "only the mutating forms are denied": that reads as licence to check for a stash before handing off, and the check itself gets refused.
- **Never switch a remote to HTTPS to work around an SSH failure.** `url.git@github.com:.insteadOf` in `~/.config/git/config` rewrites an HTTPS URL back to SSH before any transport runs, so the HTTPS remote fails with an SSH error and the change buys nothing. Fix the cause instead — most often a locked vault, the clause below.
- **A locked 1Password vault reads as a credentials problem and is not one.** The signature is `sign_and_send_pubkey: signing failed for ED25519 … communication with agent failed`, then `git@github.com: Permission denied (publickey).`, then `fatal: Could not read from remote repository.` and the stock "make sure you have the correct access rights" advice. Match the whole pairing: the advice alone reads as a bad or missing key and sends you off to regenerate one, or to try HTTPS above. And a locked vault does not look like a missing agent — it looks like a working one refusing one operation: `ssh-add -l` exits 0 and lists every identity it holds while signing is refused, so an agent check answers healthy and sends you looking elsewhere. The count is not the signal — the two machines hold different numbers of identities. Measured on natedev 2026-09-09 and 2026-09-10, and on the Mac 2026-09-10 as a same-turn pair. Unlock and re-run. Full account — mechanism, the four attempts, and the control that separates a locked vault from every other cause — in `~/nixos/docs/status.md`.

## working with the user

### iterative problem solving
When iterating on a problem that doesn't resolve within a couple of attempts, **always** create an attempts log in the project memory directory. Log every approach tried — what was changed, the reasoning, and the result. Update the log **before** moving to the next attempt. Inform the user whenever a new entry is added (e.g. "Updated attempts log — attempt #N: ...") so they know progress is being tracked without having to ask.

### renaming code
if you need something renamed such as a type or a function or whatever, the user can use the editor's ability to do a global change very quickly. in such situations, ask the user if they wish to rename the field so it can be done quickly and accurately.
