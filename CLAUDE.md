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

Every entry names either **the platform its evidence came from** — an OS denial, which cannot travel to the other machine — or **the setting it depends on**, a sandbox-config denial. `~/.claude/settings.json` is one tracked file shared by both machines, so a config-derived entry holds on both until someone commits a change, and can go stale on both at once. An entry carrying neither tag is untested: a Mac observation silently becomes a rule on Linux, and an unnecessary override costs nothing, so nothing in normal operation will ever raise the question. To tell the two apart, check whether the shared config already permits the thing — if it does and the command still fails, the denial is the OS.

- **`gh` — any command; macOS only**: the sandbox network proxy breaks TLS certificate verification (`x509: OSStatus -26276` — an `OSStatus` is a macOS type, so this could never have come from Linux). **On Linux `gh` runs sandboxed**: `gh api`, `gh repo view --json` and `gh auth status` each exit 0 with empty stderr, measured 2026-09-09. `allowedDomains` in the shared `settings.json` permits `github.com` and `api.github.com` on both machines, so what fails on the Mac is TLS underneath an allowlist that already said yes.
- **git branch-switching and worktree operations** — `checkout`, `merge`, `rebase`, `stash`, `worktree remove`; **config-derived** (`sandbox.filesystem.allowWrite`), so it holds on both machines: they rewrite or delete files outside the sandbox's allowed write paths. Untested on either machine, deliberately — testing it means running one of these sandboxed, which the rule above forbids.
- **`taplo`**, e.g. `taplo fmt` for auto-fixing; **macOS only**: panics under macOS Mach IPC restrictions (`SCDynamicStoreCreate`)
- **`diskutil` — any subcommand**, `list` included; **macOS only** (the tool itself is): the sandbox blocks the DiskArbitration Mach service, so it exits with "unable to use the DiskManagement framework" and blames single-user mode. Nothing about the message names the sandbox, and there is no path or domain to add — `/sandbox` manages writes, network and `excludedCommands`, none of which reach a Mach service. `diskutil` is in `excludedCommands`, which only spares the unsandboxed run an approval prompt; the sandboxed attempt still fails, so skip it.
- **`dscl` — any query; macOS only** (the tool and opendirectoryd both are): the sandbox blocks opendirectoryd, the directory-service Mach endpoint, and **every subcommand fails the same way and says so**. `dscl . -list /Users UniqueID` and `dscl . -read /Users/natemccoy UniqueID` both print `Operation failed with error: eServerError` and exit 70; unsandboxed the first returns 166 lines including a `natemccoy` line with UID 501, and the second `UniqueID: 501`, both exit 0. A blocked query never resembles a true negative, in either channel: an absent record exits 56 with `<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)`, an unknown node exits 185 with `-14009 (eDSUnknownNodeName)`. Measured 2026-09-09; there is no path or domain to add. Do not write `-list` back in as a silent case that returns empty with exit 0 — an earlier version of this entry claimed exactly that, and the two commands above disprove it in one run.
- **`fdesetup` — `status` included; macOS only**: same Mach-service family as `diskutil`. Sandboxed it exits 15 with `Error: Unknown volume or device specifier: '/'.`, which reads as a broken volume rather than a blocked query; unsandboxed it exits 0 with `FileVault is On.` Measured 2026-09-09.
- **`blender`, including `--background`; macOS only** (the Metal query is): segfaults during its own startup, before any `--python` script runs. The backtrace ends in `GPU_backend_type_selection_detect` → `MTLBackend::metal_is_supported` → `strstr`; the sandbox blocks the Metal device query, so a NULL device string reaches `strstr`. Reads as a Blender bug or a broken install — it is neither.
- **`codex`, and any script that launches it** — `style-eval-all.sh`, `style-fix-worktrees.sh`, `fix.sh`; **config-derived** (`sandbox.filesystem.allowWrite` admits `~/.claude`, `~/.cargo`, `~/rust` and a few more, but not `~/.codex`), so it holds on both machines: codex needs write access to `~/.codex/sessions`, which the sandbox blocks (`Operation not permitted (os error 1)`). The fix pipeline's launchd job runs outside Claude Code entirely, so the scripts themselves need no changes — this rule only affects invoking them from a session.
- **builds of crates whose build scripts call Swift Package Manager** — `apple-cf`, `apple-metal`, `screencapturekit`, generally anything wrapping a macOS framework; **macOS only**: SwiftPM sandboxes its own manifest compile with `sandbox-exec`, and **macOS sandboxes cannot nest**, so that call fails at `sandbox_apply` and the build script panics.
  - The signature is `sandbox-exec: sandbox_apply: Operation not permitted`, usually buried under a panic that names Swift and never names the sandbox — it reads like a broken dependency. **Treat it as a sandbox failure, never a code defect**: re-run unsandboxed before concluding anything, and never report it as a finding, pin or patch the dependency, or write it into a delegate prompt as a known pre-existing failure.
  - It is intermittent because build-script output caches: once built unsandboxed it stays green until a dependency bump, a toolchain change, or a manual `cargo clean` makes the scripts re-run.

### editing protected files under `~/.claude` — do NOT use the sandbox override
`~/.claude` is writable, but specific paths are carved back out: `CLAUDE.md`, `settings.json`, and the `skills`/`hooks`/`commands`/`agents` directories. Shell writes to them (`mv`, `>`, `sed -i`) fail with `Operation not permitted`.

- **Use the Edit/Write tools instead** — they route through the permission gate rather than the filesystem sandbox, so the edit lands with no override needed
- This is a deliberate guard on config files, not a proxy or IPC limitation — `dangerouslyDisableSandbox` is the wrong fix here even though it would work
