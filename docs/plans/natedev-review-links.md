# Review links: plan

> **Production:** natedev — unit herdr-unit, worktree /home/natepiano/worktrees/claude-natedev-herdr, branch natedev-herdr.

## Goal

The user asks any session, on natedev or on the Mac, for a link to an output (a doc, a report, a draft). The session prints a URL. The user clicks it in the terminal (often an ssh session from the Mac into natedev), and it opens in a browser on the Mac. User request 2026-10-29: "create an html link that i can click on to review artifacts".

## Decisions (showrunner, 2026-10-29)

1. One shared server per machine, run as a user service: a systemd user service on natedev, a launchd user agent on the Mac. It binds only to that machine's Tailscale IPv4 address and serves one folder, `~/.local/share/review-links/`, and nothing else. No `tailscale serve` change. The nixos side (natedev and Mac modules) goes to the showrunner as a diff; the showrunner commits `/etc/nixos`.
2. The skill copies a snapshot of the named files into a per-session subfolder, renders `.md` to a dark-mode, phone-width HTML page with a `<pre>` fallback, and prints the URL. Each snapshot gets a new name, so a link never changes after it is printed.
3. The service deletes files older than 7 days. A session end deletes nothing.
4. The Mac side is proved from natedev over ssh, and a link from each machine is opened from the other.

## Facts measured 2026-10-29

- natedev: `natedev.tail21e383.ts.net`, 100.103.14.30. Mac: `nates-macbook-pro-2023.tail21e383.ts.net`, 100.111.54.14. Mac python3 3.13.15 at `/run/current-system/sw/bin/python3`.
- A hand-run `http.server` on natedev bound to 100.103.14.30:8765 answered 200 to `curl` from the Mac. The natedev firewall is active, and no `tailscale0` rule names 8765 (only the Sunshine ports in `modules/linux/remote-desktop.nix`). The active firewall script (`systemctl cat firewall.service` → `firewall-start`) trusts only `lo`; `tailscale0` is not in `trustedInterfaces`. The likely path is tailscaled's own `ts-input` chain, which accepts traffic on `tailscale0` ahead of `nixos-fw` (reading it needs root: `iptables -S ts-input`). Phase 2 confirms that, and adds an explicit `networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8765 ]` unless the measurement shows that the rule cannot take effect.
- Mac application firewall: off, stealth mode off.
- natedev has no pandoc and no python `markdown` module. The page embeds the markdown text and renders it with `marked` from cdnjs (exact version, for example 12.0.2); with no network, the `<pre>` copy shows.
- A module pair to copy for both machines: `modules/linux/hanadocs-prioritize.nix` and `modules/darwin/hanadocs-prioritize.nix`.

## Phase 1 — Link maker, server and skill

**Files:** `scripts/review_links/review_link.py`, `scripts/review_links/serve.py`, `scripts/review_links/test_review_link.py`, `scripts/review_links/test_serve.py`, `commands/review_link.md`, `docs/as-built/review-links.md`.

**Work:**

- `review_link.py <file>... [--name <label>]`:
  - Copies each file into `~/.local/share/review-links/<6-char id>/` (follow-up, user 2026-10-29: "make the url short enough that it doesn't wordwrap").
    - The same set of files (resolved path and published name) keeps its id, so a second call refreshes the content behind the same link; `--new` gives a new id. The index is `.index.json` under one `flock`.
  - Renders each `.md` file to `<stem>.html`: the page title is the file name, the page has a phone-width layout, dark mode follows the system, and `<pre>` is the fallback. `.html` and images are copied as they are. Other text files are copied as they are, and the server sends them as UTF-8 text.
  - Prints one URL per file: `http://<tailscale IPv4>:8765/<id>/<file>`. The host is `tailscale ip -4`: on natedev itself the MagicDNS name resolves to a local address that the browser refuses.
  - Refuses to copy a directory or a missing file, and does nothing in that case.
- `serve.py`:
  - Binds to `tailscale ip -4`. When `tailscale ip -4` gives no address, at start or at a check once a minute, or gives a different address, it exits nonzero so that the service manager starts it again. A stopped Tailscale that still reports the same address does not stop it: the socket stays bound to that address (review ruling, 2026-10-29).
  - Serves only files under the root folder, with no directory listings: a request for a directory, or for a path that leaves the root, gets 404.
  - Content types: `.md` and `.txt` as `text/plain; charset=utf-8`, `.html` as `text/html; charset=utf-8`.
  - Deletes snapshot folders older than 7 days at start and every hour after that.
- `commands/review_link.md`: the skill. It runs `review_link.py` on the named files and puts the URLs in the reply. The user can say "make a link for X" in any session, on either machine.
- `docs/as-built/review-links.md`: what is built, the port, the folder, the sweep, and how to check the service on each machine.

**Verification:**

- `python3 -m pytest scripts/review_links -q`. The tests cover:
  - snapshot naming, and that an old snapshot is never overwritten;
  - the rendered page holds the escaped text and the fallback;
  - the URL form;
  - refusals for a directory and a missing file;
  - the server's 404 for a directory and for a path outside the root;
  - the content types;
  - the sweep keeps a 6-day folder and deletes an 8-day folder.
- `basedpyright scripts/review_links` reports 0 errors, 0 warnings and 0 notes.

**Acceptance:** run `serve.py` on a test port bound to the Tailscale IP, run `review_link.py` on `docs/herdr-issue-draft.md`, and confirm that `ssh mac curl` gets 200 and the expected title.

## Phase 2 — Services on both machines (diff to the showrunner)

**Files:** a diff for `/etc/nixos`, sent to the showrunner. No commit in `/etc/nixos` by this unit.

**Work:**

- natedev:
  - A systemd user service `review-links` that runs `serve.py` with `Restart=always`, `RestartSec=10s` and `StartLimitIntervalSec=0`. A user unit cannot order itself after the system's `tailscaled.service`, so when Tailscale is down at login the restart every 10 s is the wait, and the start limit cannot stop the unit for good.
  - The firewall line from the facts above, with a comment that records the measurement.
- Mac: a `launchd.user.agents.review-links` agent with `KeepAlive` and `RunAtLoad`. The shared job in `modules/common/review-links.nix` (`nate.jobs.review-links`, `keepAlive = true`) gives both; the darwin backend needs no file of its own, and launchd's default 10 s `ThrottleInterval` is the retry gap.
- The diff is sent to the showrunner. After it lands and both machines are rebuilt, stop the hand-run server, pid 2757978, which this session started.

**Acceptance:**

- From natedev, `curl` gets 200 from a link that the Mac's `review_link.py` printed (run over `ssh mac`).
- From the Mac (`ssh mac curl`), `curl` gets 200 from a link that natedev printed.
- Both services come back after `systemctl --user restart review-links` on natedev and `launchctl kickstart -k` on the Mac.

## Not in scope

- `tailscale serve`, HTTPS, and access from outside the tailnet.
- A live view of a file that changes. Each link is a snapshot; a new version gets a new link.

## Phase 4 — The stop deadline covers record locks

Follow-up routed from the fast shutdown and restore phase (repair round 4, finding F032; design: Astra review /tmp/claude/astra/20261029T233410Z-3175188/review.md items 1, 4 and 5). Today the bounded stop's record operations (initial updates, reads, claim, final write, and the account-hold release after `bounded_stop` returns) take blocking locks outside the absolute 66 s deadline, so a lock holder can push a correct result past it (probe: `stopped` at 80 s).

- Pass the absolute deadline through both lock layers (directory lock, then account-file lock) and the post-stop hold release, recomputing the time left at each acquisition. An acquisition timeout happens only before the mutation callback, releases any outer lock, and keeps the temporary-file replace protocol.
- Define the commit boundary: recheck the deadline before publishing the prepared record; a timeout before publishing is failure with "record not updated".
- Define the recovery for an attempt that timed out without persistence, so `next_step()` does not hold its record and account hold forever.
- Acceptance: fake-lock tests (a holder for 80 s returns failure by 66 s, no record published after the failure result; a later retry resolves the abandoned attempt). Files: scripts/shutdown/stop.py, record.py, settle.py and their tests.
- Also routed (round 5, showrunner ruling): restart.py:1458 stale timer compensation disables a newer completed restore; restart.py:2397 failed timer compensation is invisible after claim loss (phases/3/review_findings_5.txt items 6-7). Acceptance: tests for both.
- Also routed (round 5 owner check, item N3): restart.py:1470 the F038 timer recheck reads `record["login"]`; a move restore's stop writes under the target login, so the window stays open for moves. Acceptance: a move test where a takeover under the target login prevents the timer restore.
- Also routed (round 8 Astra design check, /tmp/claude/astra/20261030T014232Z-1618645/review.md items 1-3): down --all skips records whose directors left the live inventory while their workers or builds survive (settle.py:932); a marker-free child that detaches into a new scope before discovery is not attributable (stop.py:995); the identity check and kill() are separate, so a Linux signal should go through a verified pidfd (stop.py:1554). Acceptance: a test for each.
