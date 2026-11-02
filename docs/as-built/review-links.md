# Review links

Review links let a session on natedev or on the Mac show files to the user in a browser. The session copies the named files into a snapshot folder and prints one short URL per file. The user clicks the URL in a terminal, often an ssh session from the Mac into natedev, and the file opens in a browser on the Mac. The URL holds the Tailscale IPv4 address of the machine that has the files, so no file goes from one machine to the other. Markdown files open as rendered pages.

## How it works

| File | Role |
|---|---|
| `commands/review_link.md` | The `review_link` skill. It runs the publisher, checks each link with `curl` and gives the links. |
| `scripts/review_links/review_link.py` | The publisher. It copies files into a snapshot folder, renders Markdown and prints the URLs. |
| `scripts/review_links/serve.py` | The server. It serves the snapshot root on the Tailscale address and deletes old snapshots. |
| `scripts/review_links/test_review_link.py`, `test_serve.py` | unittest suites. The publisher tests use a fake `tailscale` on `PATH` and a temporary home. The server tests bind `127.0.0.1` on port 0. |
| `/etc/nixos/modules/common/review-links.nix` | The shared job `nate.jobs.review-links` for both machines. |
| `/etc/nixos/modules/linux/review-links.nix` | The natedev restart settings and the firewall line. |

Run the tests from the repository root with `python3 -m unittest discover -s scripts/review_links -p 'test_*.py'`. They need Python 3.11 or later.

The flow:

1. The skill runs `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--new]` on the machine that has the files. `scripts/lib/py` finds Python 3.11 or later.
2. The publisher checks every input, gets the host, writes the snapshot under the index lock and prints one URL per file.
3. The skill fetches each URL with `curl` on the same machine. It gives only the links that return `200`, as clickable Markdown links that show the file name.
4. The browser on the Mac gets the file from the server on that machine, over the tailnet.

### Publisher

`review_link.py FILE... [--new] [--name LABEL]`

- `--new` makes a separate link for the file set. Without it, a run on a known set refreshes its link.
- Older callers still pass `--name`. The publisher accepts it and ignores it.
- On success it prints one URL per input, in argument order, and exits 0. On an error it prints `review_link: <error>` to stderr and exits 2.

The URL form is `http://<host>:8765/<id>/<file name>`, with the name percent-encoded, for example `http://100.103.14.30:8765/k3x9qa/herdr-issue-draft.html`. The URL of a `.md` input names its rendered `<stem>.html`. The host is the output of `tailscale ip -4` (10 s timeout), parsed as IPv4. Empty output, an IPv6 address, a host name or a nonzero exit refuses the run.

Two environment variables change the defaults. Neither changes the address the server binds.

- `REVIEW_LINKS_ROOT` sets the snapshot root for both scripts. The default is `~/.local/share/review-links`. An empty value counts as unset.
- `REVIEW_LINKS_HOST` sets the URL host, and the publisher then does not run `tailscale`. It uses the value as given and removes only a trailing dot.

Each snapshot is a flat folder `<root>/<id>/`. The ID has six characters that `secrets.choice` picks from the lowercase letters and digits. An exclusive `mkdir` creates the folder. A taken ID gets a new pick, up to 10 tries, and then the run refuses. Each output keeps the name of its source file. A `.md` file (suffix in any case) gives two outputs: the source bytes and a rendered page `<stem>.html`. All other files, HTML and images included, are copied byte for byte. Files in one snapshot can link to each other by name.

The publisher refuses the whole request, and writes nothing, for:

- a path that is not a regular file, for example a missing file or a folder;
- a `.md` file that is not valid UTF-8;
- a reserved output name: `.index.json`, `.index.lock`, or a name that starts with `.review-link-`, in any case;
- two outputs whose names are equal after `str.casefold`, a generated `<stem>.html` included. The error names both source files;
- no valid Tailscale IPv4 address.

`<root>/.index.json` maps a file set to its ID. The key is the JSON text of the sorted list of `(resolved source path, published file name)` pairs, so the argument order does not change the ID. The publisher first reads the inputs, renders the Markdown and gets the host. Then it takes an exclusive `fcntl.flock` on `<root>/.index.lock`. Under that one lock it does these steps:

1. Read the index. Drop each entry whose ID is not six lowercase letters or digits, or whose folder is missing or is a symlink.
2. Reuse the folder of the set. Pick a new ID when the set is new or `--new` is set.
3. Write each output to a `.review-link-*` temporary file in the folder, then rename it into place with `os.replace`.
4. Delete the files and symlinks in the folder that are no longer outputs. Subfolders stay.
5. Set the modification time of the folder to now.
6. Write the index the same way: a temporary file, then `os.replace`.

The lock file stays on disk. Closing it releases the lock, also on an error. If an `OSError` occurs in steps 3 to 6 and the folder is new, the publisher deletes that folder and the index does not change. In a reused folder, the files already replaced keep their new bytes.

### Markdown page

- The page uses the source file name, for example `notes.md`, as its `<title>`.
- A viewport meta tag and a centered 48rem text column make it fit a phone. Long words and URLs outside tables wrap with `overflow-wrap: anywhere`.
- It follows the system light or dark mode with `color-scheme: light dark` and `light-dark()` colors.
- It loads `marked` 12.0.2 from `https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js`.
- It holds the HTML-escaped source in `<pre id="markdown-source">`. The `<pre>` shows until `marked` renders the page. It stays if the script does not load or throws an error.
- Code blocks and the `<pre>` fallback scroll sideways without wrapping. Tables are centered and use as much of the window width as their content needs; wider tables scroll inside the page. Cells wrap at spaces, never inside a word or number. Images fit the width.
- A custom `marked` renderer escapes raw HTML. A `<details>` or `<script>` block shows as text and does not run.

### Answering a change review

`change_page.py` publishes one page for one or more change-review rows.
The record's `Kind` column is the only source for each change's kind.
Use these commands:

```sh
python3 scripts/production/change_review.py set-kind --production DOC --list LIST --row C27 --kind 'See in action'
python3 scripts/production/change_review.py set-kind --production DOC --list LIST --row C28 --kind 'Read only'
python3 scripts/review_links/change_page.py --production DOC --list LIST C27 C28
```

Omit `--list` to use the production's configured list.
Pass only `C27` to publish one change.
A change that alters something the user runs or sees has the kind **See in action**.
Other changes have the kind **Read only**.
The publisher refuses rows without a valid kind, a change description, or proof in the How to check cell.
It shows both kinds.

The short URL ends in `/changes.html`.
The page uses the trial page's plain, narrow layout.
Each section shows its kind, change description once, instructions or proof, and answer controls.
The user can select Works, Not works, Validate yourself, or Send feedback.
The feedback field accepts one line, which matches the verdict command's table-cell format.
Works with text saves `works: <text>`.
Not works with text saves `not works: <text>`.
Send feedback saves the user's text without a prefix.
The existing `change_review.lands` function decides whether the verdict lands or returns the row.

**Validate yourself** sends the showrunner the row ID and **the user asks you to validate this**.
The message includes any feedback from the form.
This answer writes no verdict and runs no feature-log command.
The row stays pending.
The page calls `next_item.move_to_end` with the row key to move that row to the queue's end.
The CLI keeps its current form and still moves the pinned item.
An unknown key raises `NextError` before the queue changes.
Rows from an earlier list use their qualified keys.

A successful submission redirects to the same page.
After a verdict saves, the page shows `Saved: <answer>`.
After a delivery failure, the page shows a **Send saved answer again** button.
The button retries message delivery through the same POST path.
The server reads the saved verdict from the record and refuses replacement text during a retry.
Every GET reads the pending and Reviewed tables, with `Cache-Control: no-store`.
No JavaScript is necessary.

The publisher stores a private `.review-link-changes.json` beside a placeholder HTML file.
The manifest contains local list and production paths, selected row IDs, and a random form token.
It contains no answer or copied change content.
The server never serves the manifest.
The selected IDs form an immutable allowlist; each publication creates a new page.
Each form must contain one row, token, outcome, and feedback field.
The server returns 4xx for unknown rows, extra fields, wrong tokens, POST queries, invalid outcomes, and multiline feedback.
This includes path fields, command fields, and non-ASCII tokens.
The request cannot select a local file, command, or recipient.

The server accepts writes only when its bound address equals the current `tailscale ip -4` address.
A different bind or a failed address lookup refuses the write.
The server resolves each row's Production slug across the root production's complete hierarchy.
A child page can therefore answer sibling rows on the shared root list.
An unknown production is refused before the verdict saves.

The server follows `CloseChangeReview` in `commands/showrunner/produce.md` before it saves a new verdict.
It selects projects through `ProjectSpec.closes_by_change_review` in `scripts/feature_log/feature_log.py`.
The existing `feature_log.py project-of` command identifies the owning production's repository from its origin.
There is no second project policy list.
Projects outside that policy skip the feature-log operation.
A row with no proposal also skips the operation and saves the user's answer.
The server passes `--if-proposed` to `add`, `supersede`, and `drop`.
The writer checks proposal absence under its existing lock on the host that owns the feature log.
On the Mac, the existing CLI forwards the operation and flag to natedev through SSH.
An absent proposal prints `skipped: no proposal for <row>` and exits 0.
Malformed proposals, incompatible states, and SSH failures remain errors.
The server makes no separate proposal lookup.
A Hana list uses a bare review ID, such as `C27`.
Other lists use their filename's showrunner name, such as `natedev/C27`.
A landing answer runs `feature_log.py add --review`.
If the writer reports that the proposal requires `supersede`, the server runs that operation instead.
A returning answer runs `feature_log.py drop --review` with the saved verdict.
An operation failure keeps the row open and sends the showrunner the row ID and failure.
The server does not repeat the feature-log operation for a saved answer.

The server then runs `change_review.py verdict` with arguments from the private manifest.
It passes the user's words with `--verdict=<text>`, so text that starts with a dash remains valid.
That command remains the only answer writer.
It retains its lock, conflict checks, scratch cleanup, and list commit behavior.
Classified rows retain Kind, How to check, and Production in Reviewed.
Older rows without Kind retain their existing Reviewed table behavior.
An identical answer can retry delivery; a different saved answer is refused.

The server returns 303 as soon as `change_review.py verdict` writes the answer.
The feature-log operation still runs before the verdict.
The request thread flushes the response before it runs the existing `scripts/message/send.py` sender.
The server starts no detached shell for message delivery.
The list's `showrunner:` frontmatter names the showrunner recipient.
The saved verdict alone selects the recipients for the first send and each retry.
An exact `works` verdict goes only to the showrunner, including text from Send feedback.
Every other verdict also goes to the owning unit through the existing checkpoint route.
The route reads the merge's checkpoints, or the named commit itself when the hash names a checkpoint.
It matches the checkpoint's branch, plan, unit, or worktree name against the owning production's Units table.
An exact change trailer selects among checkpoints.
A single unit also covers a reworded row.
An ambiguous merge is refused before the save.
A change outside a unit goes to the production's configured Showrunner session.
The owning production's Showrunner checkout locates the commit.
No agent polls for answers.
A save shows **Saved: &lt;answer&gt;** from the review record.
`DeliveryStatus` owns one private delivery record per review list and row under the configured review root.
The record has separate validation and verdict slots, each with its latest attempt ID and state.
Only the validation slot stores feedback.
Before the verdict write, the server stores a pending intent in the verdict slot with a new attempt ID.
If this write fails, the server refuses the verdict write.
The record contains no saved verdict, recipient, or error text.
Validate yourself stores its feedback in the validation slot because the review record has no validation answer.
A GET selects the verdict slot when the review record has a saved verdict.
Otherwise, it selects the validation slot and keeps the answer form available.
A failed verdict write preserves the validation slot, its feedback, and its resend control.
A stop after the verdict write leaves the verdict slot available for a retry.
A pending message shows **The message may not have sent** and a resend control.
A failed saved-answer message shows **Saved, but the message did not send** and **Send saved answer again**.
A failed validation message shows **The validation message did not send** and **Send validation request again**.
The validation control retains the feedback from the intent.
Each retry reads saved verdicts from the review record.
A retry creates a new attempt ID.
One stable `fcntl.flock` protects intent creation, conditional success deletion, and conditional failure updates.
Message sends run outside the lock.
An older attempt cannot clear or change a newer attempt's record.
A successful current attempt removes only its slot and preserves the other slot.
The server deletes the record only when both slots are empty.
After successful verdict delivery, retained validation feedback does not restore the resend control.
Existing single-intent records, `failed\n` records, and validation feedback records remain retryable.

A failed POST keeps its 4xx or 503 status and uses the change page's layout and style.
A known page shows **Not saved: &lt;reason&gt;** above the row and keeps its feedback and selected outcome.
The reason uses short words; server stderr keeps the full error.
The showrunner's feature-log failure message also keeps the full error.
If the main page cannot render, a separate form retains the typed feedback and selected outcome.
This form reads only the page manifest and notice, without row or queue reads.
An unknown page, invalid content type, or refused bind shows one short sentence in the same style.

After a save or a row move, each render reads `next_item.change_queue(production)` for the remaining count.
Validate yourself keeps the row in that count.
The page reads moved rows from the existing urgency state and stores no second queue.
The Next form publishes the queue head through the existing change-page publisher and returns 303 to that page.
It uses the same urgency order, pin, and moved rows as `/next change`.
One stable lock in `urgency.py` protects pin, selection, and move operations across all queues.
Each operation opens the lock separately and retains atomic file replacement.
A GET never publishes or advances a page.
An empty queue shows **0 changes left** without a Next control.
A retry can repeat a delivered message; the existing sender's repeat rules apply.
The review record remains the only verdict store.

Restart the existing review-links service after deployment.
No NixOS or service configuration change is necessary.

### Server

`serve.py [--port PORT]`. The default port is 8765. `--port` is for tests.

- At start, the server runs `tailscale ip -4` (10 s timeout) and parses the output as IPv4. It creates the root if it is missing and runs the sweep. Then it binds a `ThreadingHTTPServer` to that address and port, and prints `review-links: serving <root> on <address>:<port>`. A failure at start, for example no address or a failed bind, prints one `review-links: <error>` line to stderr, and the server exits 1.
- Every 60 s (monotonic clock), the server runs `tailscale ip -4` again. A failed lookup prints `tailscale address gone (was <address>)`. A different address prints `tailscale address changed (was <old>, now <new>)`. In both cases the server exits 1, and the service manager starts it again on the current address. If Tailscale stops but still reports the same address, the server keeps running, because its socket stays bound to that address.
- The server percent-decodes the path and drops the query. A path with a `..` segment gets 404. The server resolves the path under the root and follows symlinks. The result must be a regular file under the root. A folder (the root `/` included), a target outside the root, the resolved `<root>/.index.json` and a name that starts with `.review-link-` get 404. The server never lists a folder.
- A published `changes.html` with a private manifest is rendered from the review record. Every other regular file gets 200 with `Content-Type`, `Content-Length`, `Last-Modified` and `X-Content-Type-Options: nosniff`. The server never sends 304.
- Content types: `.html` and `.htm`, in any case, go out as `text/html; charset=utf-8`. A type that Python's `mimetypes` lists under `image/` keeps that type. All other files, `.md`, `.txt`, `.json` and `.pdf` included, go out as `text/plain; charset=utf-8`.
- Each request writes the standard access line to stderr.

### Sweep

`sweep_snapshots(root: Path, now: float) -> int` deletes expired snapshot folders and returns how many it deleted. The server runs it at start, before the bind, and then once an hour.

- A folder expires when its modification time is more than 7 days before `now`. Each publish resets that time, so a link expires 7 days after its last update. With the hourly sweep, a link lasts up to one hour more while the server runs. A stopped server sweeps at its next start.
- At the root, the sweep checks `is_symlink()` before `is_dir()`, so it never follows a symlink. It skips symlinks and entries that are not folders, for example `.index.json`, `.index.lock` and temporary files.
- A root folder is a flat snapshot when its name is six lowercase letters or digits and it holds no subfolder and no symlink. The sweep deletes a flat snapshot as one folder.
- Any other root folder is a session folder of the older `<session>/<UTC stamp>-<label>-<unique suffix>/` layout. The sweep checks each child folder on its own, skips child symlinks and never deletes the session folder.
- An `OSError` on the root listing, on an entry check or on a delete prints `review-links sweep: <path>: <error>` to stderr, and the sweep goes on to the next entry. A folder that is gone before its delete is skipped with no line. The hourly call has one more `OSError` catch around the whole sweep.
- The sweep does not take the index lock and does not change the index. The next publish drops the entries of deleted folders.

### Services

Both machines import `/etc/nixos/modules/common/review-links.nix`. It defines the job `nate.jobs.review-links` with `keepAlive = true` and the script `exec python3 "$HOME/.claude/scripts/review_links/serve.py"`. The job runs the script from the `~/.claude` checkout, not from a Nix store copy, with the user's `PATH`, which has `python3` and `tailscale`. The backends `/etc/nixos/modules/linux/nate-backend.nix` (systemd) and `/etc/nixos/modules/darwin/nate-backend.nix` (launchd) turn the job into the service on each machine. The services set no `REVIEW_LINKS_ROOT` and no `REVIEW_LINKS_HOST`.

- natedev: the systemd user unit `review-links`. The backend sets `Type=simple`, `Restart=always` and `WantedBy=default.target`. `modules/linux/review-links.nix` adds `RestartSec=10s`, `StartLimitIntervalSec=0` and the firewall line `networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8765 ];`. Logs go to the user journal.
- Mac: the launchd user agent `org.nixos.review-links`. The backend makes `launchd.user.agents.review-links` with `KeepAlive = true` and `RunAtLoad = true`. It sets no `Label`, so the nix-darwin prefix `org.nixos` names the agent. There is no darwin-only module. The default launchd `ThrottleInterval` of 10 s is the restart gap. stdout and stderr go to `~/Library/Logs/nate-jobs/review-links.log`.

## Invariants

- Ordinary snapshot content changes only when the same file set is published again without `--new`. Change pages read their selected rows live from the change review record. Their publisher writes only a private manifest and placeholder into the snapshot folder; the sweep deletes whole folders.
- A new ID never takes an existing folder. The exclusive `mkdir` refuses a taken ID.
- Each output and the index change by one `os.replace` of a complete temporary file in the same folder. A reader gets the old bytes or the new bytes, never part of a file.
- Every index read, ID pick, snapshot write, cleanup and index write happens under the one `flock` on `<root>/.index.lock`.
- A refused request writes nothing. The input checks and the host lookup run before the publisher creates the root or takes the lock. The one exception is the 10-tries ID refusal: it can leave the root and `.index.lock`, but no snapshot and no index change.
- New snapshot folders hold only regular files. The flat-or-legacy test of the sweep depends on this.
- Temporary files start with `.review-link-`, and the publisher refuses inputs with that prefix or with an index name. The 404 rule of the server and the skip rule of the sweep depend on these names.
- The server binds only the IPv4 address from `tailscale ip -4`. `--port` changes only the port. The server exits 1 when that address is gone or changes, and it never serves on an old address.
- The server serves regular files under the resolved root and the selected rows of published change pages, and never lists a folder. It never serves index files, page manifests or unfinished writes, including names in a different letter case.
- One failed sweep entry never stops the bind, the server or the sweep of other entries. The sweep never follows a symlink.
- Only the 7-day sweep deletes snapshots. A session end deletes nothing.
- The skill gives a link only after `curl` on the same machine returns `200`, and only for the review-links service, never for a hand-run server.

## Gotchas

- **Test port.** The publisher always prints port 8765, so a test server on another `--port` is not at the printed link.
- **Mac case-insensitive filesystem.** The publisher compares output names after `str.casefold`, which is full Unicode case folding. `Report.txt` and `report.txt` collide. `Straße.txt` and `STRASSE.txt` collide. `Review.md`, with its `Review.html`, collides with `review.HTML`. This stops one output from overwriting another on the Mac.
- **Private filenames.** `private_name` blocks index names and the `.review-link-` prefix using case folding, before and after resolving symlinks. This includes page manifests on both machines.
- **Symlink aliases.** The key uses the resolved path and the published name. A symlink with a different name gets its own ID and its own copy, and the original link stays. A path through a symlinked folder to a file with the same name reuses the ID.
- **The legacy `manual/` folder.** `manual` is six lowercase letters, so it matches the ID pattern. A new snapshot never holds a subfolder or a symlink. So a six-character folder that holds one is a legacy session, and the sweep checks its children one by one. A `manual/` that holds only a symlink keeps it. Empty legacy session folders stay.
- **Links between Markdown files.** A `.md` output goes out as `text/plain`, so a link to `other.md` opens the raw text. Link to `other.html` for the rendered page.
- **Raw HTML.** GitHub renders raw HTML such as `<details>`, and this page shows it as text. Keep a draft for GitHub in plain Markdown, so it looks the same in both places.
- **PDF and other binary files** go out as `text/plain; charset=utf-8` with `nosniff`, so the browser shows them as text.
- **`--new`.** Later runs without `--new` refresh the new folder. The old folder keeps its content until the sweep deletes it, and no run can refresh it again.
- **Atomic per file, not per bundle.** During a refresh of several files, a reader can see one new file next to one old file.
- **A bad index stops all publishing.** If `.index.json` is not valid JSON or is not a JSON object, every run exits 2 until someone repairs or removes the file.
- **`REVIEW_LINKS_ROOT` in a session.** A session that sets it publishes to a folder that the service does not serve, and its links return 404. The `curl` check finds this.
- **Code changes need a restart.** A change to `serve.py` needs no rebuild, but a running server keeps the code it started with. Restart the service after a change to `serve.py`. A change to `review_link.py` takes effect at the next run.
- **Sweep and refresh at the same moment.** The sweep takes no lock. A refresh of the folder that the sweep deletes at that moment can fail, or can print a link that returns 404. The `curl` check finds this, and a second run gets a new ID.

## Why

- **An IPv4 literal, not a MagicDNS name.** On natedev, `/etc/hosts` maps the short name `natedev` to local addresses, for example `127.0.0.2`, where the server does not listen. So a `curl` check or a browser on natedev fails with that name. A bare `natedev` URL also failed in the user's browser on the Mac, although the Mac resolver returned the Tailscale address of natedev. The literal is the address that the server binds.
- **A short URL.** The URL must not wrap in a terminal. The target is 60 characters. The IPv4 address of each machine has 13 characters, so a 20-character file name gives 53 characters, and a test checks the limit. The full name `natedev.tail21e383.ts.net` gives 65 characters. Percent-encoding makes names longer: a space becomes `%20`, and each non-ASCII letter becomes 6 or more characters.
- **Plain HTTP on the tailnet, no `tailscale serve`.** The server binds only the Tailscale address, so only tailnet devices can reach it. One user service per machine needs no change to the Tailscale configuration. HTTPS, `tailscale serve` and access from outside the tailnet are out of scope.
- **`marked` in the browser.** natedev has no `pandoc` and no Python `markdown` module, so the publisher does not render Markdown itself. The escaped `<pre>` keeps the page readable with no network.
- **Snapshots and change pages.** Ordinary files are copied into the served root and stay after their publishing session ends. Change pages instead show only the review rows authorized by their private manifests. Both expire through the 7-day sweep.
- **Refresh in place.** A second run on the same file set updates the same link, so the user refreshes the page that is already open to see an edit. `--new` keeps the old content when a separate link is necessary.
- **Exit 1 on an address change.** A socket stays bound to one address, and only a new process can bind a new address. Exit 1 makes the service manager start the server again on the current address.
- **`StartLimitIntervalSec=0` and `RestartSec=10s`.** When Tailscale is down at login, the server exits 1 at once. A user unit cannot order itself after the system's `tailscaled.service`, so the restart every 10 s is the wait. The systemd defaults, a 100 ms restart delay and a limit of five starts in ten seconds, would stop the unit for good before the tailnet returns.
- **The firewall line.** Port 8765 on the Tailscale address of natedev answered the Mac with no firewall rule for it. The active `firewall-start` script trusts only `lo`, so the probable path is the `ts-input` chain of tailscaled. Nobody has read that chain, because it needs root (`iptables -S ts-input`). The rule states the intent and keeps the port open if that chain changes.
- **The script from `~/.claude`.** Script updates reach both machines with no rebuild.
- **The symlink check comes first in the sweep.** A legacy session can hold only links to targets that the sweep cannot read. The sweep checks for a link first, so it does not follow the link or fail on it.

## Service checks

natedev:

```sh
systemctl --user status review-links
journalctl --user -u review-links -n 30
systemctl --user restart review-links
```

The journal shows the start line `review-links: serving <root> on <address>:8765` and one access line per request. It shows a `review-links sweep:` line for each entry that the sweep skips on an error, and a `review-links: tailscale address ...` line before each exit on an address loss.

Mac (from natedev, put each command in `ssh mac '...'`; the single quotes make `$(id -u)` run on the Mac):

```sh
launchctl print gui/$(id -u)/org.nixos.review-links
launchctl kickstart -k gui/$(id -u)/org.nixos.review-links
tail -n 30 ~/Library/Logs/nate-jobs/review-links.log
```

Before you give a link, fetch it on the machine that printed it:

```sh
curl -s -o /dev/null -w '%{http_code}' '<URL>'
```

Give the link only if the result is `200`. To see the page title too, run `curl -s '<URL>' | grep -o '<title>[^<]*</title>'`. To check from the other machine, run the same `curl` there, for example from natedev: `ssh mac "curl -s -o /dev/null -w '%{http_code}' '<URL>'"`. If a link fails, compare the URL host with `tailscale ip -4`, and check that the service is active.
