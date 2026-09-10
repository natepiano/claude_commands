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
- **NEVER poll a backgrounded command.** No `sleep`/`grep`/`tail` loop, no `for i in $(seq …); do sleep 5; done`, no repeated reads of the output file to detect completion. If I find myself writing a wait loop, that's the bug — delete it and just wait for the notification. Foreground `sleep` is sandbox-blocked anyway.
- **Do NOT foreground-block a long command** (a plain Bash call with a big `timeout`) as the way to "wait" for it. Background it and yield the turn instead. Foreground is for fast commands (seconds) whose output I need inline to proceed.
- After the `<task-notification>` arrives, read the output file once to get the result. Capturing exit code: pipelines mask it — `cargo … | tail` reports `tail`'s exit (always 0), so a real failure looks like success. Use `set -o pipefail`, or don't pipe the command whose exit code matters. This is not only about long commands — it holds for **any** command whose exit status I intend to read, a one-off diagnostic probe included.
- **Never `${PIPESTATUS[0]}`.** That is bash's spelling, and both machines default to zsh, where the array is `pipestatus` — lowercase, and **1-indexed**. `${PIPESTATUS[0]}` expands to the empty string with no error and no warning, so `[ "${PIPESTATUS[0]}" -ne 0 ]` gets an empty operand instead of a status, and correcting only the case still reads element 0 of a 1-indexed array. `set -o pipefail` is the remedy to keep; reach for `${pipestatus[1]}` only when one specific element of a pipeline is genuinely what I want. Measured 2026-09-09, when the form this bullet used to recommend returned an empty answer while checking a `git fetch` — the exact failure the rule above exists to prevent, produced by the rule that teaches it.

## bevy BRP MCP
- when the user says "launch", just launch the app directly — don't try to shut down first. The user has already shut it down.
- when the user says "relaunch", shut down the app first, then launch it.

## working with the user

### iterative problem solving
When iterating on a problem that doesn't resolve within a couple of attempts, **always** create an attempts log in the project memory directory. Log every approach tried — what was changed, the reasoning, and the result. Update the log **before** moving to the next attempt. Inform the user whenever a new entry is added (e.g. "Updated attempts log — attempt #N: ...") so they know progress is being tracked without having to ask.

### renaming code
if you need something renamed such as a type or a function or whatever, the user can use the editor's ability to do a global change very quickly. in such situations, ask the user if they wish to rename the field so it can be done quickly and accurately.

## sandbox

### commands that must run unsandboxed
**ALWAYS** pass `dangerouslyDisableSandbox: true` from the start for every case below, **on the platform that entry names**. Do NOT try sandboxed first — they always fail there. `excludedCommands` never helps: it decides whether an *unsandboxed* run needs approval, not whether a command runs sandboxed. No other setting fixes these.

**How to read a tag.** An entry names the platform its evidence came from, or **config-derived** (traceable to a key in the shared `settings.json`, so it holds on both machines until someone commits a change) or **built-in** (Claude Code's own protection of `~/.claude`, which no setting or commit changes and which can differ between the two machines at the same moment). A **Linux** row means *no override needed here* — never *the Linux sandbox permits this*, since exit 0 in a default call cannot separate a live sandbox permitting a command from one that never applied to it. An untagged entry is untested, and an unnecessary override costs nothing.

**Measurements, derivations and withdrawn claims live in `~/.claude/docs/sandbox_evidence.md`.** Read that before changing an entry, disputing one, or adding one — and put new evidence there rather than restating it here.

- **`gh` — any command; macOS only**: the sandbox proxy breaks TLS certificate verification (`x509: OSStatus -26276`). **Linux: no override needed.** If the failure instead names `~/.local/state/gh-1password/last-use` with `Operation not permitted`, that is the nix wrapper dying before the real `gh` runs — so read the path inside an `Operation not permitted` and check it is even the thing you were reaching for.
- **`git` over the network** — `fetch`, `push`, `ls-remote`, `pull`; **macOS only.** Exit 128 under `fatal: Could not read from remote repository.` plus the stock access-rights advice, which reads as a credentials problem and is not one. **Match on that pairing, never on the transport line above it**: three messages (broken pipe, proxy authentication, DNS) come back nondeterministically for the one cause. **Linux: no override needed.** A locked 1Password vault is a *different* failure (`sign_and_send_pubkey: signing failed`) that a sandboxed run can never show you, because the sandbox stops the connection before signing is attempted — so re-run unsandboxed before concluding anything about either the remote or the agent.
- **git operations that rewrite the working tree** — `checkout`, `merge`, `rebase`, `worktree remove`. **In `~/.claude` on macOS this is measured**: a sandboxed `git rebase` dies at `unable to unlink old 'CLAUDE.md': Operation not permitted` and never starts, because git must replace a **built-in**-protected file inside an otherwise-writable tree. It does not reproduce on Linux, where the same unlink is permitted. **Everywhere else it stays a conservative default rather than a measured denial** — neither `~/nixos` nor `/etc/nixos` holds a protected path for a checkout to trip over — but keep the override on both machines anyway: it costs nothing, and a rebase halted partway costs real recovery.
- **`git stash` — do not add it back.** It is in `permissions.deny` (`Bash(git stash)`, `Bash(git stash *)`, `Bash(git stash:*)`), and `dangerouslyDisableSandbox` does not bypass a permission denial, so an override could never have worked. **The `allow` entries for `git stash list` do not make the read form runnable** — `Bash(git stash *)` matches it too, and deny wins. Do not soften this into "only the mutating forms are denied": that reads as licence to check for a stash before handing off, and the check itself gets refused.
- **`taplo`**, e.g. `taplo fmt` for auto-fixing; **macOS only**: panics under macOS Mach IPC restrictions (`SCDynamicStoreCreate`).
- **`diskutil` — any subcommand**, `list` included; **macOS only**: the sandbox blocks the DiskArbitration Mach service, so it blames single-user mode and names nothing about the sandbox. There is no path or domain to add, and its presence in `excludedCommands` does not help.
- **`dscl` — any query; macOS only**: the sandbox blocks opendirectoryd, the one endpoint every query goes through, so **every subcommand fails identically** — `Operation failed with error: eServerError`, exit 70. A blocked query never resembles a true negative: an absent record exits 56, an unknown node 185. Do not write `-list` back in as a silent case returning empty at exit 0; it does not.
- **`fdesetup` — `status` included; macOS only**: same Mach-service family as `diskutil`. Sandboxed it exits 15 with `Error: Unknown volume or device specifier: '/'.`, which reads as a broken volume rather than a blocked query.
- **`blender`, including `--background`; macOS only**: segfaults during its own startup, before any `--python` script runs, because the sandbox makes `MTLCreateSystemDefaultDevice()` return nil and this build predates the upstream nil check. Both a sandbox effect and a Blender bug, not a broken install; past that upstream fix anything wanting the GPU still needs the unsandboxed run.
- **`codex`, and any script that launches it** — `style-eval-all.sh`, `style-fix-worktrees.sh`, `fix.sh`; **config-derived**, so it holds on both machines: codex needs to write `~/.codex/sessions`, and `~/.codex` is not in `allowWrite`. The fix pipeline's launchd job runs outside Claude Code, so this only affects invoking them from a session.
- **builds of crates whose build scripts call Swift Package Manager** — `apple-cf`, `apple-metal`, `screencapturekit`, generally anything wrapping a macOS framework; **macOS only**: SwiftPM sandboxes its own manifest compile, **macOS sandboxes cannot nest**, and the build script panics at `sandbox_apply`. The signature is `sandbox-exec: sandbox_apply: Operation not permitted`, usually buried under a panic that names Swift and never the sandbox, so it reads like a broken dependency. **Treat it as a sandbox failure, never a code defect**: never report it as a finding, pin or patch the dependency, or write it into a delegate prompt as a known pre-existing failure. Intermittent because build-script output caches.

### editing protected files under `~/.claude` — do NOT use the sandbox override
`~/.claude` is writable, but specific paths are excluded from that: `CLAUDE.md`, `settings.json`, and the `skills`/`hooks`/`commands`/`agents` directories. Shell writes to them (`mv`, `>`, `sed -i`) fail with `Operation not permitted`.

- **Use the Edit/Write tools instead** — they route through the permission gate rather than the filesystem sandbox, so the edit lands with no override needed
- This is a deliberate guard on config files, not a proxy or IPC limitation — `dangerouslyDisableSandbox` is the wrong fix here even though it would work
- It also catches `git` when the repo *contains* those paths, which is why a rebase in `~/.claude` needs the override — see the working-tree entry above
