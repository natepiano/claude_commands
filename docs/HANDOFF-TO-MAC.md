# For the Mac session, from natedev (NixOS) — 2026-09-03

Delivered as a file because `SendMessage` cannot reach this session's name from
here (tried "~/.claude session" and "claude session"; both "not reachable").
Delete this file once you have read it.

## 1. Your sidecar snapshot had never once run on Linux

The git-filters watcher was silently dead here, and the failure was reported as
success. Fixed in `/etc/nixos`; no repo change was needed.

The systemd `.path` unit that plays the role of your launchd `WatchPaths` job
ran `refresh_filtered_index.sh` with a unit PATH containing only git. That
script's shebang is `#!/usr/bin/env bash`, so `env` has to FIND bash on PATH
before the script executes a single line. Every invocation died at exec with:

    env: 'bash': No such file or directory

and the unit had `ExecStart=-`, whose leading `-` tells systemd to report a
nonzero exit as `Result=success`. It looked healthy in `systemctl --user status`
the whole time.

Broken from the `444800d` shebang conversion (`#!/bin/bash` ->
`#!/usr/bin/env bash`) until 2026-09-03. Two independent causes, both fixed: the
unit now exports the full user PATH (it also needs `jq` for the sidecar), and
the `-` is gone so a real failure shows up in `systemctl --user --failed`.

**Why it matters to you:** the broken window covers your
`settings_local_keys_snapshot` work. On this machine the sidecar was never
written by the watcher, so the smudge filter had nothing current to merge back
— the exact "model keys silently lost on the next pull" failure the sidecar
exists to prevent, hidden by the `-`. Verified working now: a write to
`settings.json` fires the unit and it runs to completion, exit 0.

Worth checking the equivalent on your side. If the Mac plist redirects stderr to
a log nobody reads, or swallows the exit code, the same class of silent death is
possible there. `refresh_filtered_index.sh` itself needed no change — it already
builds PATH additively rather than pinning homebrew, which is what let it work
under both launchd and systemd once the caller stopped starving it.

## 2. A real bug I introduced, now fixed — please pull, it touches the Mac

Commit `58406ec`. During the portability sweep I wrote:

    CACHE_DIR="${XDG_CACHE_HOME:-${HOME}/Library/Caches}/hanadocs-prioritize"

That looks portable and is not. `XDG_CACHE_HOME` is normally UNSET — the spec
says fall back to `~/.cache` — so on Linux the fallback silently reproduced the
macOS layout. It did: the first NixOS run created `~/Library/Caches`, a
directory nothing else on the box owns.

It now picks the root per platform. Darwin still resolves to `Library/Caches`,
deliberately, so the Mac's existing `semantic-inputs.json` keeps being found;
changing it there would orphan the snapshot and force a needless full re-score.
**Nothing should change on the Mac — that is the point.** The fixture in
`tests/test_run_watcher.py` matches that line by exact string, so it moved in
lockstep. 40 tests OK on Linux.

## 3. Status: the launchd port is DONE, 7 of 7

`hanadocs-prioritize` was the last. `/etc/nixos/modules/hanadocs-prioritize.nix`,
`Type=simple` + `Restart=always`. Verified live: active, `NRestarts=0`, and one
second after start it scored and ranked all 336 open issues, ranks 1..336
canonical, snapshot committed.

One deliberate non-obvious choice, since the plist tempts the other way: I did
NOT set `IOSchedulingClass=idle` next to `LowPriorityIO`. Your own plist comment
records why — `ProcessType=Background` put the job in the DARWIN_BG band, whose
per-write throttle turned a ~330-file renumber from 0.5s into 38-47s. Linux's
idle class is the same trap under sustained I/O, and a renumber storm is
sustained I/O. It uses best-effort / prio 7 + `Nice=5` instead.

`install_watcher.sh` and `status_watcher.sh` now guard on `command -v launchctl`
(copying your `ensure_git_filters.sh` pattern) and point at the nix module on
Linux. Both behave identically on macOS.

I also deleted `docs/CROSS-PLATFORM-HANDOFF.md` (commit `8957efc`) now that the
inventory it tracked is finished — content is still in history. One open item
outlived the doc and moved to the task list rather than being dropped:
`scripts/release/restore_unreleased.sh` uses `\n` in a sed replacement, which
GNU sed expands and BSD sed does not. Pre-existing, unrelated to the port, and
the Mac is where it misbehaves.

## 4. One thing I did not touch, in case it is yours

`settings.json` reads as modified here, but it is NOT a filter failure —
`model`/`effortLevel` are correctly absent from the diff. The diff is a genuine
key reorder: `blockReadsOutsideWorkingDirectories` moved to after
`additionalDirectories`. Something rewrote the file in a different key order.
jq preserves key order, so `settings_local_keys_restore` is not the obvious
culprit, but you know that code better than I do. Left alone, given the standing
instruction not to hand-edit `settings.json`.
