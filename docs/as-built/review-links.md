# Review links

`commands/review_link.md` publishes review artifacts from either machine. The command runs `scripts/review_links/review_link.py FILE... [--name LABEL]` and returns one clickable URL per file, using this machine's MagicDNS name from `tailscale status --self --json`.

Snapshots live under `~/.local/share/review-links/<session>/<UTC stamp>-<label>-<unique suffix>/`. The session is the first eight characters of `CLAUDE_CODE_SESSION_ID`, or `manual`. Every invocation creates a new folder; later source edits and session shutdown leave earlier links unchanged. All inputs are checked before writing. Missing files, directories and filename collisions refuse the entire request. Output names are compared with Unicode case folding so collisions are also refused on the Mac's case-insensitive filesystem; the error names both source files.

Markdown is copied alongside a rendered `<stem>.html` page. The page uses the filename as its title, a responsive layout, system light/dark colors and marked 12.0.2 from cdnjs. An escaped `<pre>` remains readable when the renderer cannot load. HTML, images and other files retain their bytes. Files in the same snapshot can refer to each other by filename.

`scripts/review_links/serve.py` binds only to the IPv4 address returned by `tailscale ip -4`, on port **8765**. It exits nonzero if that lookup or binding fails. While serving, it repeats the address lookup every 60 seconds. A failed lookup or a different address stops the server with exit status 1 and one stderr line describing the lost or changed address. This relies on the systemd user service or launchd user agent restarting a failed process so it can bind the current Tailscale address. A Tailscale that is stopped but still reports the same address does not stop the server: the socket stays bound to that address, so the server answers again when Tailscale is back, as a restart would. `--port PORT` selects a test port. It serves regular files only under the snapshot root; directories, parent traversal and symlinks leading outside the root return 404. HTML is UTF-8 `text/html`, images keep their MIME type, and other files are UTF-8 `text/plain`.

At startup and every hour the server deletes snapshot folders whose modification time is more than seven days old. Session folders remain. `REVIEW_LINKS_ROOT` overrides the storage root for both scripts; `REVIEW_LINKS_HOST` overrides the link maker's hostname for tests. Neither changes the server's Tailscale-only binding.

## Service checks

Service integration belongs to phase 2. The intended Linux user service is `review-links`; check it with `systemctl --user status review-links` and `journalctl --user -u review-links -n 30`. The intended Mac launchd user agent is `review-links`; check it with `launchctl print gui/$(id -u)/org.nixos.review-links` and restart it with `launchctl kickstart -k gui/$(id -u)/org.nixos.review-links` after that agent is installed.

For a direct check, run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/serve.py --port 8766`, publish a Markdown file, replace `:8765` with `:8766` in its URL, and fetch it from the other machine with `curl -f`. Production links use plain HTTP within the tailnet; no `tailscale serve` configuration changes are needed.

On 2026-10-29 a temporary server bound to natedev's Tailscale address `100.103.14.30:18765`. The Mac at `100.111.54.14` fetched a snapshot of `docs/herdr-issue-draft.md` over SSH with curl, received HTTP 200, and read `<title>herdr-issue-draft.md</title>`. The temporary server and its isolated snapshot root were removed afterward. Installed-service checks remain for service integration.
