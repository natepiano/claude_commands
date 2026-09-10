# Sandbox evidence

`~/.claude/CLAUDE.md`, under *commands that must run unsandboxed*, carries the
operative rules: which command, on which platform, what signature to match, and
what to pass. This file holds the measurements those rules were derived from,
and the claims that were made, tested and withdrawn along the way.

**Read this when you are changing an entry, disputing one, or adding one** — not
at session start. It is referenced from CLAUDE.md by plain path and deliberately
**not** imported with `@`. An `@` import would load it into every session on both
machines, which is the cost the split exists to remove.

Both machines share this file through `claude_commands`. Where an entry names a
platform, that is the machine its evidence came from.

## How entries are tagged, and how each tag goes stale

Three sources can deny a command, and they fail differently over time.

**OS denial** (`macOS only`, `Linux`). A `sandbox-exec` result belongs to the
machine that produced it. It goes stale when the OS moves.

**Config-derived.** Traceable to a key in `~/.claude/settings.json`, which is one
tracked file shared by both machines — so the entry holds on both until someone
commits a change, and can go stale on both at once, leaving a diff behind when it
does.

**Built-in**, and it is the worst of the three to let go stale. Claude Code
protects parts of `~/.claude` itself — `CLAUDE.md`, `settings.json`, and the
`skills`/`hooks`/`commands`/`agents`/`projects` directories — with no setting
behind it and no OS involved. It goes stale with a Claude Code release, can
differ between the two machines at the same moment, and has no shared file to
inspect.

To separate a config denial from an OS one: check whether the shared config
already permits the thing. If it does and the command still fails, the denial is
the OS.

**Evidence read from a source travels between machines; evidence measured does
not.** An upstream commit, a source file, or a config quoted by key applies to
both, so the Linux session can support a **macOS only** entry by citation even
though it can never reproduce the failure there. That is the only kind of support
either machine can supply for the other's platform, so prefer it where it exists.
Check a citation against the local machine before relying on it — the `blender`
entry cites an upstream fix that applies only because the local build predates it.

### Reading a Linux row

Every Linux row in the section means **no override needed here**, never *the
Linux sandbox permits this*. natedev corrected two of its own rows on this point
(2026-09-10) after writing "On Linux `gh` runs sandboxed" when what it had
measured was that `gh` works.

**The cause is now known, and it replaces the limit this section used to stop
at.** The old wording said only that exit 0 in a default call cannot separate a
live sandbox permitting the command from one that never applied — a statement
about what a reader may conclude. It can now be put positively: **the sandbox
cannot engage on the Linux machine at all.** Claude Code's Linux sandbox needs
`bwrap` and `socat`, and neither is on `PATH` there — bubblewrap sits in the nix
store only as a dependency of flatpak, fwupd, gnome-desktop and
xdg-desktop-portal, never installed into a profile, and nothing in `/etc/nixos`
installs either binary. Claude Code says so at startup: *sandbox is enabled but
dependencies are missing … Commands will run WITHOUT sandboxing. Network and
filesystem restrictions will NOT be enforced.* So `sandbox.enabled: true` and the
whole `allowWrite`/`allowedDomains` block are inert there, and every Linux
measurement either session has ever taken was taken in an unsandboxed shell.

natedev found it 2026-09-10 while reproducing the Mac's `continue`-alias defect,
which launched a nested Claude that printed the warning neither session had seen
before. It checked the two binaries directly rather than resting on the message.

**Every operational conclusion survives; only the reasoning beneath them is
replaced.** "No override needed here" was correct each time it was written. So is
the reading rule, now for a better reason: a Linux row still never means *the
Linux sandbox permits this*, because there is nothing there to permit it.

### A negative result carries the moment it was taken

Measured 2026-09-10 on this Mac. A peer named a commit, `cf35d6ac`, and I
reported it nonexistent on three checks: absent from `git cat-file` in all 17
checkouts on this machine; HTTP 422 `No commit found for SHA` from `gh api
repos/natepiano/hana/commits/cf35d6ac`, and the same for `bevy_brp`; and not the
`headSha` of the CI run that had been cited. Every one was correct when taken.
Two were false within minutes — the commit was real, pushed from a machine that
is not this one, shortly after I asked.

**What each negative measured, against what it was reported as:**

- the local sweep measured *absent from this Mac*, and was read as *absent*
- the 422 measured *not pushed as of the instant of that call*, and was read as
  *nonexistent*
- the `headSha` check concerned a fixed property of a completed run, and it held

Only the third was about something that could not move underneath the answer.

**This and the Linux row fail alike and need different cures** — natedev's
distinction, and the reason the two sit next to each other. A stale negative was
a real measurement of a real thing, so re-running it repairs it. A Linux row was
never a measurement of what the reader took it for, so re-running it a hundred
times yields the same exit 0 and the same wrong reading. Refresh the first;
re-derive the second.

**A third case wears the costume of the first, and the cure above fails on it.**
Later the same day natedev cited `3ce55ad` as the commit fixing the alias. `gh
api` returned the same HTTP 422, and by the rule as written the cure was to
re-run it after the push landed. That would have returned 422 forever. The commit
was real and already pushed — as `6ee27b7`. natedev had committed `3ce55ad`,
rebased onto my push to send it, and quoted the pre-rebase SHA. `3ce55ad` had
stopped being the name of a reachable object the moment it rebased, and survives
only in its reflog.

So a **stale negative** and a **permanently void reference** produce an identical
422, and the question that separates them is not *when did I measure* but **is
the string I am asking about still the name of anything**. A SHA quoted by a peer
who has since rebased is not. Two cheap checks: `git merge-base --is-ancestor
<sha> origin/main`, or match on the **commit subject** rather than the SHA — the
subject survives a rebase and the SHA does not. Prefer the subject when a peer
hands you a hash for work it has not yet pushed, since that is exactly the hash
most likely to be rewritten before it arrives.

The staleness here sat in the **citation**, not in the measurement, and the
measurement was what made it findable: reporting *422 as of 11:07* sent natedev
to check its own reference, where *the commit does not exist* would have sent it
looking for a push failure. Report the negative with its moment attached even
when you are confident, because the timestamp is what lets someone else locate
the error in their half.

**How to apply:** date a negative when you record one, and never let a negative
about a shared remote stand as a property of the world. Before acting on one,
re-run it — and if it is about an identifier a peer supplied, check that the
identifier still resolves before concluding anything about the thing it names.
Name the machine and the moment it belongs to. The positive form needs none of
this care: a commit that exists cannot stop existing underneath you, though the
name you were given for it can.

### The built-in category is measurable in one direction only

Writing to two paths inside one allowed tree shows a denial directly:
`~/.claude/.probe` succeeded and `~/.claude/projects/.probe` exited 1 with
`operation not permitted`. The path is the only difference, so that needs no
separate proof the run was sandboxed.

**Both succeeding proves nothing** — an absent policy and a policy that did not
apply are identical from inside. This is the ambiguity the paired probe exists to
remove, and it removes it on one side only.

**Exit 1 is not by itself a denial.** `skills`, `hooks` and `agents` do not exist
under `~/.claude` on the Linux machine at all, so a probe aimed at one exits 1
saying `no such file or directory`, which a status check alone reads as a
refusal. Confirm the path exists first and read the message, not the status:
`operation not permitted` is the only thing that settles it.

The category is also readable where the harness states an effective policy — the
Mac's session lists the protected paths under `denyWithinAllow` — but **not every
session is given one**. No such block reaches the Linux session, which is itself
the sharpest evidence that the two machines are not carrying the same object.
Run on both machines 2026-09-09; the denial fired on the Mac only.

## `gh`

The Mac's failure is TLS certificate verification through the sandbox network
proxy: `x509: OSStatus -26276`. `OSStatus` is a macOS type, so this could never
have come from Linux. `allowedDomains` permits `github.com` and `api.github.com`
on both machines, so what fails is TLS underneath an allowlist that already said
yes.

Linux: `gh api`, `gh repo view --json` and `gh auth status` each exit 0 with
empty stderr in an ordinary call, measured 2026-09-09.

### The TLS error was masked by a nearer one for some time

On 2026-09-10 every sandboxed `gh` on the Mac died at:

    /etc/profiles/per-user/natemccoy/bin/gh: line 16:
    /Users/natemccoy/.local/state/gh-1password/last-use: Operation not permitted

exit 1 — `gh auth status` and `gh api rate_limit` alike. `gh` there is a nix
wrapper that pulls a token from the keychain or 1Password, then, under `set -eu`,
truncates a marker file into `~/.local/state/gh-1password/`. That directory was
absent from `allowWrite`, so the write failed and `set -e` killed the wrapper
**before the real `gh` binary ever ran**.

Adding `~/.local/state/gh-1password` to `sandbox.filesystem.allowWrite` moved the
failure to `Get "https://api.github.com/rate_limit": tls: failed to verify
certificate: x509: OSStatus -26276` — the entry's actual claim. The entry was
right the whole time; its evidence had stopped being reachable.

Two things generalize past `gh`:

- **A wrapper's failure can impersonate the tool's.** Read the path inside an
  `Operation not permitted` and ask whether it is even the thing you were
  reaching for. `~/.local/state/gh-1password/last-use` is not a network path and
  never was.
- **An `allowWrite` edit takes effect live.** The session's stated policy block
  listed the new path in the same turn, no restart. A filesystem hypothesis is
  cheap to test rather than argue about.

natedev's framing of the pair: this finding and its own `gh` row are one shape
from opposite sides — a true claim whose evidence had stopped being reachable,
against reachable evidence supporting a smaller claim than the one written down.

## Network git

`fetch`, `push`, `ls-remote`, `pull`. macOS: every one fails sandboxed against
`github.com` at exit 128. Unsandboxed the same commands exit 0; a `git push` to
`origin` succeeded unsandboxed in the same session that a `git fetch` failed
sandboxed (2026-09-10).

### The transport message is nondeterministic

Three are on record for the one cause:

- `ssh_dispatch_run_fatal: Connection to UNKNOWN port 65535: Broken pipe`
- `Received disconnect from UNKNOWN port 65535:1: This proxy requires
  authentication, and this client did not offer an authentication method, so the
  connection was refused.`
- `Could not resolve hostname github.com: -65563` (recorded in
  `~/nixos/docs/status.md`, credential-gate section)

The first two came back from *the same* `git ls-remote origin HEAD`, nothing
changed between them, minutes apart on 2026-09-10. An earlier draft read the
proxy-authentication wording as belonging to one particular test condition; that
was a coincidence, not a condition. Match on exit 128 paired with `fatal: Could
not read from remote repository.` and treat the line above it as noise.

### Why no single setting opens it

SSH is refused at three independent layers, which is also why the message varies
— whichever reports first is the one you see:

- raw TCP connect to `140.82.116.4:22` → `PermissionError [Errno 1] Operation not
  permitted`
- `socket.gethostbyname('github.com')` cannot resolve
- the egress proxy answers `This proxy requires authentication, and this client
  did not offer an authentication method, so the connection was refused`

The third is the sharpest: the proxy wants an auth method `ssh` has no way to
present. That is architecture, not an allowlist gap. Auth would be dead anyway —
the 1Password `IdentityAgent` socket (`ssh-add -l` → `Error connecting to agent:
Operation not permitted`) and the `ControlMaster` socket under `~/.ssh` are both
denied. **Do not chase this by adding `~/.ssh` or the agent socket to
`allowWrite`**: credential paths do not belong in the allowlist, and the network
layer would still refuse.

### Everything reaches GitHub over SSH, including the HTTPS URLs

`~/.config/git/config` carries `url.git@github.com:.insteadof =
https://github.com/`, so an `https://github.com/...` remote is rewritten to SSH
before any transport happens. That is why an HTTPS-looking `git ls-remote` still
fails with an *SSH* error, and why "switch the remote to HTTPS" is not the fix it
appears to be.

HTTPS itself is fine, which is what makes the SSH error confusing: `curl
https://github.com` returns 200 sandboxed, and `GIT_CONFIG_GLOBAL=/dev/null git
ls-remote https://github.com/rust-lang/log.git HEAD` exits 0 with a real SHA —
dropping the global config drops the rewrite, so that one goes over HTTPS and
works.

It is still not a usable workaround for a private repo, but **not for the reason
an earlier draft gave**. That draft cited `fatal: failed to store: 100001` from
the osxkeychain helper. That line is emitted by the `rust-lang/log` run too — the
*successful* one, printed alongside the SHA it just fetched — so it is noise on
every sandboxed HTTPS fetch and can never be what blocks one. What actually
blocks a private repo is having no credential: `GIT_TERMINAL_PROMPT=0 … git
ls-remote https://github.com/natepiano/hana.git HEAD` exits 128 at `could not
read Username for 'https://github.com': terminal prompts disabled`. Same
conclusion, different cause, and the difference is what tells you a public repo
would work.

### Linux does not reproduce any of it

natedev ran the same three-layer probe rather than only running git, so both
machines compare on one measurement (2026-09-10):

- `socket.gethostbyname('github.com')` → `140.82.112.3`
- raw TCP connect reaches port 22 — the Mac's literal `140.82.116.4` and the
  resolved address both — and 443
- `ssh-add -l` lists both identities instead of `Operation not permitted`
- `git fetch origin` over `git@github.com:` returned `04bbea4..cd80862`, exit 0

Linux is not behind the Mac's egress proxy. Per the reading rule above, the
measured claim is *the ordinary run works, pass no override*; *the Linux sandbox
permits network git* is not measured and is not claimed.

## git operations that rewrite the working tree

`checkout`, `merge`, `rebase`, `worktree remove`.

### The measured denial, macOS, scoped to `~/.claude`

`git rebase origin/main` in `~/.claude`, sandboxed, 2026-09-10:

    error: unable to unlink old 'CLAUDE.md': Operation not permitted
    error: could not detach HEAD

No rebase started; unsandboxed the same command succeeded immediately.

**The mechanism is not the one this entry asserted for a day.** The old text
reasoned from writes landing outside `allowWrite`. That was never it: git failed
writing *inside* an allowed tree, to a path the **built-in** protection covers.
`CLAUDE.md` is in `denyWithinAllow`, git had to replace it to move HEAD, and
could not. Right conclusion, wrong stated reason.

That narrows the scope of what is measured. The denial needs a repo that
*contains* protected paths, and `~/.claude` is the only one either machine works
in that does. `/etc/nixos` and `~/nixos` contain none, so there is no protected
file for a rebase there to unlink. This rules out *this* denial elsewhere, not
some other one — and neither checkout is in `allowWrite` either — so the override
stays a conservative default outside `~/.claude`.

### Linux does not reproduce it

natedev, default Bash call, no override, backup taken first, `git diff
--exit-code` clean afterward and no residue:

    mv ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.probe   →  ALLOWED, restored
    touch + rm ~/.claude/commands/.probe-unlink        →  ALLOWED both

Same operation, same file, same shared checkout: denied on the Mac, allowed on
Linux. This is the strongest evidence either machine has produced for the
per-machine divergence of the built-in category — a matched pair on one
operation rather than two separate observations.

The asymmetry runs the usual way and it favors the Mac: **the denial firing
proves the policy is live there; the non-denial proves nothing**, since it cannot
separate an absent policy from one that did not apply. The pair is one
measurement and one silence.

### Why it sat untested, and why that reason is gone

The entry read `still untested by design` from 2026-09-09 to 2026-09-10. The
worry was that testing it meant risking a rebase halted partway, which costs real
recovery. The Mac's run is the counter-evidence: it refused *before starting*,
detached no HEAD, and cost nothing. That is what let the entry become measured
instead of assumed.

Two config facts that came out of the same reading and are worth keeping:
`sandbox.filesystem.allowWrite` does not contain either checkout (`/etc/nixos`,
`~/nixos`) — both are writable as the working directory, a grant appearing
nowhere in `settings.json` — and there is **no filesystem deny list anywhere in
the shared config**. The entire `sandbox.filesystem` block is `allowWrite` and
nothing else, which is what establishes the `~/.claude` protection as a built-in
rather than a setting, and why no commit changes it.

## `git stash`

Removed from the override list on 2026-09-09; do not add it back. It is denied in
`permissions.deny` (`Bash(git stash)`, `Bash(git stash *)`, `Bash(git stash:*)`),
and `dangerouslyDisableSandbox` does not bypass a permission denial — so the
advice to run it unsandboxed could never have worked. It also contradicts the
standing instruction never to stash.

**The `allow` entries for `git stash list` do not make the read form runnable.**
`permissions.allow` carries `Bash(git stash list)` and `Bash(git stash list *)`;
`permissions.deny` carries `Bash(git stash *)`, which also matches them. Deny
wins, and an attempt to run `git stash list` on 2026-09-09 was refused. Do not
soften this into "the read is permitted, only the mutating forms are denied" —
that reads as licence to check for a stash before handing off, and the check
itself gets refused.

`merge`, `rebase` and `worktree` sit in `permissions.allow` and are pre-approved,
which is a separate matter from whether they are sandboxed.

## `diskutil`

macOS only (the tool is). The sandbox blocks the DiskArbitration Mach service, so
it exits with "unable to use the DiskManagement framework" and blames single-user
mode. Nothing in the message names the sandbox, and there is no path or domain to
add — `/sandbox` manages writes, network and `excludedCommands`, none of which
reach a Mach service. `diskutil` is in `excludedCommands`, which only spares the
unsandboxed run an approval prompt; the sandboxed attempt still fails.

## `dscl`

macOS only (the tool and opendirectoryd both are). The sandbox blocks
opendirectoryd, the directory-service Mach endpoint — which is why **every
subcommand fails the same way and says so**. The block is at the one endpoint
every query goes through, not at a particular subcommand.

`dscl . -list /Users UniqueID` and `dscl . -read /Users/natemccoy UniqueID` both
print `Operation failed with error: eServerError` and exit 70. Unsandboxed the
first returns 166 lines including a `natemccoy` line with UID 501, the second
`UniqueID: 501`, both exit 0. A blocked query never resembles a true negative in
either channel: an absent record exits 56 with `-14136 (eDSRecordNotFound)`, an
unknown node exits 185 with `-14009 (eDSUnknownNodeName)`.

### A warning that invented the danger it warned about

CLAUDE.md used to say `-read` at least errors while `-list` returns empty at exit
0, making `-list` the silent case to fear. Both exit 70 with the same message, so
the difference does not exist. This is one step past a claim reaching beyond its
evidence: the claim reached onto a case that behaves identically, and the warning
never had anything to warn about.

The pressure that produces this is that advice needs a dangerous variant in order
to be advice, so an author who has measured one command and is writing a general
rule will supply the variant rather than measure the second command. What made it
findable was that the entry quoted `eServerError` for `-read` and quoted nothing
for `-list` — the unquoted half was the false half. Corrected at `2f9891b`.

## `fdesetup`

macOS only; same Mach-service family as `diskutil`. Sandboxed it exits 15 with
`Error: Unknown volume or device specifier: '/'.`, which reads as a broken volume
rather than a blocked query. Unsandboxed it exits 0 with `FileVault is On.`
Measured 2026-09-09.

## `taplo`

macOS only. Panics under macOS Mach IPC restrictions (`SCDynamicStoreCreate`).

## `blender`

macOS only (the Metal query is). Segfaults during its own startup, before any
`--python` script runs. The backtrace ends in `GPU_backend_type_selection_detect`
→ `MTLBackend::metal_is_supported` → `strstr`.

The sandbox blocks the Metal device query, and that half is measured rather than
inferred: `MTLCreateSystemDefaultDevice()` returns `nil` sandboxed and `Apple M2
Max` unsandboxed, **both exit 0 with nothing on stderr** (three-line Swift probe,
2026-09-09). A nil device has no name, so a NULL string reaches `strstr`.

The last step is confirmed upstream rather than inferred: Blender fixed exactly it
in `19726af88c03` (2026-09-03, issue #163272, `mtl_backend.mm` only), and the
guard now in its source carries the comment `MTLCreateSystemDefaultDevice() may
return nil in sandboxed or non-GUI contexts`. So this is **both** a sandbox effect
and a Blender bug, not a broken install: the sandbox supplies the nil, and a
missing nil check is what turned it into a segfault.

The citation is checked against the local machine rather than assumed to apply.
The Mac's binary is Blender 5.0.0, build commit 2025-11-18, `a37564c4df7a`,
`blender-v5.0-release` — ten months before the fix, so the guard really is absent
here. Past that fix the symptom changes but the rule does not: the commit says
Blender falls back to a dummy device and logs `no GPU functionality will work`
and `scripts relying on GPU might crash later on`, so anything wanting the GPU
still needs the unsandboxed run.

## `codex`

Config-derived, so it holds on both machines: `sandbox.filesystem.allowWrite`
admits `~/.claude`, `~/.cargo`, `~/rust` and a few more, but not `~/.codex`.
codex needs write access to `~/.codex/sessions`, which the sandbox blocks
(`Operation not permitted (os error 1)`). Covers any script that launches it —
`style-eval-all.sh`, `style-fix-worktrees.sh`, `fix.sh`.

The fix pipeline's launchd job runs outside Claude Code entirely, so the scripts
themselves need no changes; this only affects invoking them from a session.

## Crates whose build scripts call Swift Package Manager

macOS only. `apple-cf`, `apple-metal`, `screencapturekit`, generally anything
wrapping a macOS framework. SwiftPM sandboxes its own manifest compile with
`sandbox-exec`, and **macOS sandboxes cannot nest**, so that call fails at
`sandbox_apply` and the build script panics.

The signature is `sandbox-exec: sandbox_apply: Operation not permitted`, usually
buried under a panic that names Swift and never names the sandbox — it reads like
a broken dependency.

It is intermittent because build-script output caches: once built unsandboxed it
stays green until a dependency bump, a toolchain change, or a manual `cargo clean`
makes the scripts re-run.
