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

Run the tests from the repository root with `~/.claude/scripts/lib/py -m unittest discover -s scripts/review_links -p 'test_*.py'`. They need Python 3.12 or later.

The flow:

1. The skill runs `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--new]` on the machine that has the files. `scripts/lib/py` finds Python 3.12 or later. Inline programs import from the shim’s repository root through `PYTHONPATH` and `-P`. File scripts keep their existing import path behavior.
2. The publisher checks every input, gets the host, writes the snapshot under the index lock and prints one URL per file.
3. The skill fetches each URL with `curl` on the same machine. It gives only the links that return `200`, as clickable Markdown links that show the file name.
4. The browser on the Mac gets the file from the server on that machine, over the tailnet.

### Publisher

`review_link.py FILE... [--new] [--name LABEL]`

- `--new` makes a separate link for the file set. Without it, a run on a known set refreshes its link.
- Older callers still pass `--name`. The publisher accepts it and ignores it.
- On success it prints one URL per input, in argument order, and exits 0. On an error it prints `review_link: <error>` to stderr and exits 2.

The URL form is `http://<host>:<port>/<id>/<file name>`, with the name percent-encoded.
The `server_port` setting starts at 8765.
For example, a rendered link is `http://100.103.14.30:8765/k3x9qa/herdr-issue-draft.html`.
The URL of a `.md` input names its rendered `<stem>.html`.
The host comes from `tailscale ip -4`, parsed as IPv4.
The `tailscale_timeout_seconds` setting starts at 10.
Empty output, an IPv6 address, a host name, or a nonzero exit refuses the run.

Two environment variables change the defaults. Neither changes the address the server binds.

- `REVIEW_LINKS_ROOT` sets the snapshot root for both scripts. The default is `~/.local/share/review-links`. An empty value counts as unset.
- `REVIEW_LINKS_HOST` sets the URL host, and the publisher then does not run `tailscale`. It uses the value as given and removes only a trailing dot.

Each snapshot is a flat folder `<root>/<id>/`.
The ID has six lowercase hex characters from `secrets.token_hex`.
An exclusive `mkdir` creates the folder.
If an ID already exists, the publisher tries another ID.
The `snapshot_allocation_tries` setting starts at 10.
After that many tries, the publisher refuses the run.
Each output keeps the name of its source file.
A `.md` file, with a suffix in any case, gives two outputs: the source bytes and a rendered page `<stem>.html`.
The publisher copies all other files, including HTML and images, byte for byte.
Files in one snapshot can link to each other by name.

The publisher refuses the whole request, and writes nothing, for:

- a path that is not a regular file, for example a missing file or a folder;
- a `.md` file that is not valid UTF-8;
- a reserved output name, in any case: `.index.json`, `.index.lock`, the `.review-link-` prefix, or an atomic temporary name;
- two outputs whose names are equal after `str.casefold`, a generated `<stem>.html` included. The error names both source files;
- no valid Tailscale IPv4 address.

`<root>/.index.json` maps a file set to its ID. The key is the JSON text of the sorted list of `(resolved source path, published file name)` pairs, so the argument order does not change the ID. The publisher first reads the inputs, renders the Markdown and gets the host. Then it takes an exclusive `fcntl.flock` on `<root>/.index.lock`. Under that one lock it does these steps:

1. Read the index. Drop each entry whose ID is not six lowercase letters or digits, or whose folder is missing or is a symlink.
2. Reuse the folder of the set. Pick a new ID when the set is new or `--new` is set.
3. Write each output through `atomic_file` to a temporary file in the same folder. Publish it with `os.replace`.
4. Delete the files and symlinks in the folder that are no longer outputs. Subfolders stay.
5. Set the modification time of the folder to now.
6. Write the index through `atomic_file` to a temporary file in the same folder. Publish it with `os.replace`.

The lock file stays on disk. Closing it releases the lock, also on an error. If an `OSError` occurs in steps 3 to 6 and the folder is new, the publisher deletes that folder and the index does not change. In a reused folder, the files already replaced keep their new bytes.

### Markdown page

- The page uses the source file name, for example `notes.md`, as its `<title>`.
- A viewport meta tag and a centered 48rem text column make it fit a phone. Long words and URLs outside tables wrap with `overflow-wrap: anywhere`.
- It follows the system light or dark mode with `color-scheme: light dark` and `light-dark()` colors.
- It loads `marked` 12.0.2 from `https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js`.
- It holds the HTML-escaped source in `<pre id="markdown-source">`. The `<pre>` shows until `marked` renders the page. It stays if the script does not load or throws an error.
- Code blocks and the `<pre>` fallback scroll sideways without wrapping. Tables are centered and use as much of the window width as their content needs; wider tables scroll inside the page. Cells wrap at spaces, never inside a word or number. Images fit the width.
- A custom `marked` renderer escapes raw HTML. A `<details>` or `<script>` block shows as text and does not run.

### Questions, Changes, and Rebuilds

The review page has Questions, Changes, and Rebuilds type tabs with waiting counts.
Each type tab shows its own count. The page has no combined total.
The heading follows the selected type: Questions to answer, Changes to review, or Rebuilds needed.
The document title remains Changes to review.
The page updates itself through its existing GET URL without a full reload.
`page_refresh_seconds` sets the interval from 5 to 300 seconds; it starts at 15.
The page loads updates only while its browser tab is visible.
It loads an update at once when the tab becomes visible again.
Updates retain the selected type, showrunner, waiting card, open Details sections, and scroll position.
If the card no longer waits, the page selects the next waiting item in the same tab.
Typed text or an active save holds replacement; the page keeps only the newest loaded update.
The page applies that update when all unsaved fields are empty or the save ends.
A successful save requests a fresh update so an older response cannot restore the saved item.
Updates preserve a retained field's focus and cursor.
A failed load changes nothing; the next interval tries again.
Button borders use light and dark colors with at least 3:1 contrast against the button fill and page background.
Questions and Changes show **Showrunner** before their staff-based tabs and show one card at a time.
Switching types retains each type's selected showrunner and card.
The URL fragment records the type, showrunner, and card.
Skip for now skips a card without saving an answer.
The control hides when no other item waits in the selected showrunner tab.
An empty type shows **No questions to answer.** or **No changes to review.**

Questions read the canonical records through `update_registration.question_records_for_document`.
A question belongs to the showrunner whose record owns it.
A question without a staff owner appears under **Owner unavailable**.
The reader also scans question folders under the showrunner state root when their owners leave staff.
Owned cards show the title before the filed text, choices, and **Asked by <session>**.
They have no separate Question label.
The field label stays **Answer**, and its action stays **Save answer**.
An **Owner unavailable** card shows the question and `No showrunner owns this question.`
It has no answer form, and the server refuses an answer.
It has no Ask showrunner to check or Ask a question controls.
`update_registration.answer` records the answer for its owning production.
The typed `AnswerRelay` gives the same recipient decision as the CLI relay line.
A unit relay receives the answer directly.
A transferred unit's showrunner receives the answer with the unit's name.
An answer without a relay goes to the question's showrunner.
Each message starts with `From the user (via the review page): ` and names the question.

A saved answer opens the next waiting question and shows **Answer saved.** above the card.
A failed save retains the typed answer and shows **Not saved: <reason>**.
An answer to a question already answered elsewhere fails without sending another message.
Question answers use the existing delivery budget, pause, claims, startup recovery, and failure alert.
Private delivery records retain the answer text, message, source record, owning document, and fixed recipient ids for recovery.
The first delivery record stores `saved: false` before the answer saves.
The second delivery record stores `saved: true` after the answer saves.
A save error clears that attempt's delivery intent and releases its claim.
Recovery sends only answers with `saved: true` whose canonical question record is closed.
Recovery clears an intent for an open question.
For a closed question without `saved: true`, recovery clears the intent and prints `answer save state unknown; not sent`.
The Changes tab retains its existing cards, controls, and count rules.

Rebuilds shows one block per machine, with natedev first, then the Mac, then other machines in name order.
It has no showrunner tabs, card navigation, Skip for now, or answer form.
Its count is the number of machines with open rebuild needs.
Each block starts with `Rebuild natedev` or `Rebuild the Mac` and tells the user where to run `rebuild`.
The Mac instruction states that updates can take five minutes while the Mac is reachable.
A machine with multiple needs shows `One rebuild applies all listed changes.`
Under `After the rebuild`, bullets show the stored reasons unchanged, newest first by creation time.
A closed `Details` disclosure shows each full configuration commit in the same order.
It shows `Affected session: <name> (<kind>)` from the current staff record.
It shows `Requested by: <name> (<kind>)` only for a different, recorded requester.
Unknown staff IDs appear as `Unknown session: <id>`.
The reader uses the same staff record as the page routing.
Each render calls `rebuild.need.read_needs()` once.
The final line states `Completed requests clear automatically. Refresh this page after the rebuild.`
An empty or missing needs folder shows only `No rebuilds needed.` beneath the tabs, with a count of 0.

The page heading uses 26px text; machine and card headings use 20px text; body text uses 17px.
The content column stays at 680px, with 8px control gaps, 16px content gaps, and 32px between machine blocks.
Controls wrap on narrow screens, keep a minimum height of 44px, and show keyboard focus in both color themes.

### Answering a change review

`change_page.py` publishes one page with every waiting change from every configured showrunner list.
Run this command without a row count or batch size:

```sh
scripts/lib/py scripts/review_links/change_page.py
```

The page finds list paths through `change_review.lists_directory()` and the existing filename prefix.
Each GET reads each list once through `change_review.review_snapshot`.
The snapshot, pending-row reader, and saved-answer reader use one table reader.
This reader checks each table row against its header's cell count.
It refuses a malformed row instead of silently losing cells.
The browser receives every waiting row in that response.
It shows one row at a time.
The page resolves production documents from the update timers, staff records, and the list's production references.
A list with no available production document retains its rows with a notice and no answer controls.
The page never uses a review list as a production document.

Tabs show each showrunner and its waiting count.
`scripts/production/change_routing.py` owns the routing rule for the page, the status line, and `/next`.
Each row stores one fixed staff id in the `Producer` cell.
The cell stores no name.
The router reads the name and parent ids from the staff record at use.
A unit producer belongs to its current staff showrunner.
A showrunner producer belongs to that showrunner.
An empty Producer cell belongs to the list's showrunner.
A rename or a removed worktree does not change the producer id.
An unknown id or a broken parent chain belongs to `unrouted`.
The page shows its note under the change.
The router prints the note and change id to stderr.
A verdict on an unrouted row saves without sending a message or recording a delivery failure.
An unrouted row offers no question control.
The server refuses its question submissions without saving.
The Production cell supplies no tab or message recipient.
The router alerts the configured showrunner about each new unrouted row and reason only within the real change-review folder.
The list's resolved parent must equal `change_routing.configured_lists_directory().resolve()`.
The `lists_directory` setting names this folder.
`CHANGE_REVIEW_DIR` is a test override for the reader folder.
This override never makes a list real.
Lists outside this folder print the routing note without sending an alert or writing an alert record.
It alerts only from the production staff record; a `STAFF_FILE` override never alerts.
`unrouted_alert_recipient` starts at `natedev` in `config/review_links.conf`.
A routed change goes to this root showrunner when its list has no staff showrunner or its production document is gone.
An unknown producer id or a broken parent chain keeps the change unrouted.
An unrouted change uses the list's document when available.
Otherwise, it takes only the root showrunner's document and keeps its owner.
The page, the verdict and the footer counts use the same owner.
The router keeps an available document, including a document that an earlier verdict added to the list.
Otherwise, the router uses the root showrunner's current document.
If neither document is available, the page shows the unavailable message.
If the root showrunner is unavailable, the router keeps the original owner.
The router reads the root setting once when it first needs this rule.
`unrouted_alert_retry_seconds` starts at 60 and accepts 5 through 3600 seconds.
The router reads both keys when it uses them.
A private record stores the last attempt time and accepted alert for each row and reason.
A nonblocking exclusive lock prevents concurrent renders from sending the same alert twice.
The router marks the alert only after the sender accepts or queues it.
A failed alert prints one stderr line and remains pending until the configured pause ends.
An alert configuration or storage failure does not stop routing.
A new reason permits one new alert.
A crash between acceptance and the durable mark can repeat an alert.
Routed rows and rows that need no user answer send no routing alert.

The status count watches the routing configuration, staff record, review lists, and delivery records.
The owner's staff document supplies the production document when the file exists.
The supplied production document for that showrunner is the fallback.
A missing document reference in the source list does not block a row with an available owner document.
An unrouted row retains verdict controls when its list's staff showrunner has an available document.
In that case, its owner remains unrouted and receives no message.

`change_review.list_ready`, which `merge_checkpoint` and `merge_gate` call, stores each checkpoint unit's Producer id.
`merge_gate` reads the staff record before the merge.
`merge_checkpoint` reads the staff record before the push.
An unreadable staff record stops either run before anything is pushed.
`merge_checkpoint` also undoes its unpushed local merge.
One read supplies the rows for each run.
Each writer matches the branch to a unit staff row through its worktree branch.
An exact Branch name wins over a Plan, Unit, or worktree alias.
The staff lookup prefers present worktrees, then the latest row within each worktree state.
An unregistered unit writes an empty cell and one named note.
`change_review.py add --repo <directory>` stores the unit id of that worktree.
A showrunner directory or an unowned directory stores an empty cell.
A verdict retains the Producer cell in Reviewed.
Delivery records store fixed staff ids.
Each send reads `staff.snapshot()` and sends to `session:<current_session>` from the saved staff row.
An account move changes the next recipient address without changing the stored staff id.
The status line counts the same staff tabs and shows a separate `unrouted` item.
The `unrouted` count covers all page lists across independent production roots.
The page and status line use the same waiting rule.
Saved verdicts, validation requests, and open questions leave the waiting count and Skip for now order as soon as they save.
The page, footer, and `/next` use this rule regardless of delivery results.
Empty lists and showrunners retain their zero-count tabs.
Tabs with waiting changes precede empty tabs in alphabetical order and keep their positions until the next page load.
The selected tab uses `outline-offset:-2px` to keep its full outline visible inside the scroll container.
Builds rows can remain in the natedev list while the builds tab shows them.

Each tab orders its prepared change rows with `urgency.queue_order`, the same order that `/next change` uses.
The page keeps row entries during ordering and does not format the text that `/next` prints.
The existing urgency order, pin, dependencies, and moved rows determine its order.
Rows from another list use qualified row IDs and qualified prerequisite IDs.
Skip for now selects the next prepared row in the browser without a server request.
The URL fragment stores the showrunner and row key.
A reload restores that place when the row waits, awaits validation, or has an open question.
An answered row stays absent from the browser list during delivery and after a reload.
An empty showrunner tab shows **No changes for this showrunner.** or **No questions for this showrunner.** and hides Skip for now.
The showrunner tab supplies the count; the card has no separate remaining count.
Each successful save opens the next waiting change in the same tab.
Skip for now skips a change without saving an answer.
The page keeps the current row if the user selects Skip for now or another tab during the save.

The record's `Kind` column remains the only source for the kind.
Set it with `change_review.py set-kind --production <showrunner name> --row C27 --kind 'See in action'`.
Use **See in action** for a change to something the user runs or sees.
Use **Read only** for other changes.
Both kinds appear.
An older row without Kind appears as **Kind not recorded**.
The page shows every row with its recorded kind or the missing-kind notice.

The short URL ends in `/changes.html`.
The page retains the plain, narrow layout and fits a phone.
Each card starts with its title.
One closed **Details** disclosure holds the ID, date, commit, and kind.
How to check or Proof stays in the main view.
The title uses 20px text; section labels use 17px text.
A 16px gap separates the title and metadata.
How to check and Proof show text lines in preformatted blocks.
Consecutive lines that start with `- ` form a bulleted list.
Consecutive lines that start with a number, a dot, and a space form a numbered list.
Text without list lines stays in one block with all whitespace unchanged.
In text with list lines, blank lines separate blocks and leading spaces are dropped.
The list file stores cell line breaks as `<br>` and reads them as line breaks.
A typed `<br>` takes one more backslash in storage and reads back unchanged.
Every line separator that Python's `splitlines` recognizes is a line break.
Each row stays on one table line.
Works and Does not work are the main actions.
Ask showrunner to check and Ask a question are secondary actions.
The **Comment or question** field is a textarea with `feedback_rows` rows.
The setting starts at 5.
The box accepts line breaks for every outcome.
The server converts CRLF, CR, and Unicode line separators to newline characters.
The verdict cell joins stripped, nonblank lines with one space.
The message and a question keep the normalized line breaks.
Works with text saves `works: <text>`.
Does not work with text saves `not works: <text>`.
The page has no Send feedback control.
Each verdict saves the text from **Comment or question** as its comment.
The existing `change_review.lands` function decides whether the verdict lands or returns the row.

**Ask showrunner to check** sends the showrunner the row ID and **the user asks you to validate this**.
The message includes any feedback from the form.
This answer writes no verdict and runs no feature-log command.
The current page advances when the request saves.
The review list retains the row until the showrunner saves a verdict.
A direct link shows **The showrunner will validate this** without answer buttons.
The delivery record preserves this state across page loads and server restarts.
The page calls `next_item.move_to_end` to move the row to the end of the showrunner's own queue.
The CLI keeps its current form and still moves the pinned item.
An unknown key raises `NextError` before the queue changes.
Rows from an earlier list use their qualified keys.

**Ask a question** uses the **Comment or question** field.
The server refuses empty questions and questions that contain only whitespace.
It saves no verdict and runs no feature-log command.
The server stores the question before it sends one message to the tab's showrunner.
The message names the row, list, production, change, and question.
It includes a separate `Question id: <id>` line.
The page advances after the save.
An open question survives page loads and server restarts.
A failed HTTP save keeps the row waiting with its typed question and answer form.
A direct link shows the open question without verdict buttons.

The showrunner answers with `scripts/review_links/change_page.py answer-question --production <showrunner name> --list <list> --row C<n> --question <id> --text "<answer>"`.
The command refuses a row without an open question.
The command refuses an old or unknown question ID.
The same ID and answer text change nothing and exit 0 on a rerun.
The same ID with different answer text is refused.
The row returns to its tab's count and Skip for now order after the answer saves.
The page shows the question and answer below the change, with the usual answer buttons.
Question and Answer show text lines in paragraphs and use the same list forms as How to check.
The answer keeps its internal line breaks and strips only whitespace at its two ends.
All text is escaped, including text in list items.
The user can ask another question or save a verdict.

Each answer uses a background `fetch` POST.
The row shows **Saving** during a save.
An automatic move shows **Answer saved.** or **Review saved.** above the next card.
Skip for now stays available during a save.
A failed save retains the feedback and selected outcome on that row.
A failed save keeps the row in that count and does not change the current row.

The all-list publisher stores a private `.review-link-browser.json` beside a placeholder HTML file.
This manifest contains a random page secret and an optional initial row key.
It stores no copied rows or answers.
Each prepared row receives a token signed from that secret and its qualified list/row key.
The server accepts only a row with that token and a row that remains in a configured list.
The request cannot select a local path, command, or recipient.
The server never serves private manifests.
Each form contains only row, token, outcome, and feedback.
Each browser form names its page as the save URL.
Unknown rows, extra fields, wrong tokens, POST queries, and invalid outcomes receive 4xx.
Path fields, command fields, and non-ASCII tokens also receive 4xx.

Older selected-row links still work with their existing plain HTML forms and 303 responses.
Their `.review-link-changes.json` manifest keeps its immutable row allowlist.
The selected-row publisher still accepts `--production <showrunner name> [--list LIST] C27`.
It requires a valid kind, description, and proof for each selected row.
The normal command uses the all-list page instead.

The server accepts writes only when its bound address equals the current `tailscale ip -4` address.
A different bind or a failed address lookup refuses the write.
The server resolves each row's Production staff name across the root production's complete hierarchy.
A child page can therefore answer sibling rows on the shared root list.
An unknown production is refused before the verdict saves.

The server follows `CloseChangeReview` in `commands/showrunner/produce.md` before it saves a new verdict.
It selects projects through `ProjectSpec.closes_by_change_review` in `scripts/feature_log/feature_log.py`.
`feature_log.project_of` identifies the owning production's repository from its origin.
There is no second project policy list.
Projects outside that policy skip the feature-log operation.
A row with no proposal also skips the operation and saves the user's answer.
The server calls `feature_log.land_review` and `feature_log.drop_review` with `if_proposed=True`.
The writer checks proposal absence under its existing lock on the host that owns the feature log.
On the Mac, each function forwards the matching `feature_log.py` command to natedev through SSH and captures its output.
An absent proposal returns `skipped: no proposal for <row>` and is not an error.
Malformed proposals, incompatible states, and SSH failures raise `FeatureLogError`.
The server makes no separate proposal lookup.
A Hana list uses a bare review ID, such as `C27`.
Other lists use their filename's showrunner name, such as `natedev/C27`.
A landing answer calls `land_review`, which lands the proposal with the verb that the proposal records (`add` or `supersede`).
A returning answer calls `drop_review` with the saved verdict.
An operation failure keeps the row open and sends the showrunner the row ID and the `FeatureLogError.diagnostic` text.
The server does not repeat the feature-log operation for a saved answer.

The server then calls `change_review.save_verdict` with paths from the configured list and production records.
It returns `VerdictSaved` with the output lines, or `VerdictRefused` with the refusal text.
`save_verdict` is the only answer writer, for the page and for the `change_review.py verdict` command.
It retains its lock, conflict checks, scratch cleanup, and list commit behavior.
Classified rows retain Kind, How to check, and Production in Reviewed.
Older rows without Kind retain their existing Reviewed table behavior.
An identical saved answer changes nothing and sends nothing; a different saved answer is refused.

The feature-log and verdict calls run in the server process on the request's HTTP worker thread.
They start no child Python process.
They change no cwd, environment, `sys.argv`, stream or process umask, so other request threads are safe beside them.
Each call takes its own `flock` on a new open file.
`end_row_scratch` can still start `folder_audit.py` as a child process when a row's Cargo checkout has a build folder outside it.

The all-list page returns JSON with status 200 after `change_review.save_verdict` writes the answer.
Older selected-row pages retain their 303 response.
The feature-log operation still runs before the verdict.
The request thread flushes the response before it runs the existing `scripts/message/send.py` sender.
The server starts no detached shell for message delivery.
The all-list page sends to the row's current staff showrunner.
Older selected-row pages use the same staff route.
The saved verdict selects recipients when a new message attempt begins.
An exact `works` verdict goes only to the showrunner, including requests from the older feedback endpoint.
When one staff unit owns the change, every other verdict also goes to that unit.
The Producer cell identifies that unit through its fixed staff id.
An empty Producer cell belongs to the list's staff showrunner.
Retries retain stored staff recipient ids.
Older records without recipient ids compute the same recipients as a fresh verdict send.
No agent polls for answers.
A save shows **Saved: &lt;answer&gt;** from the review record.
`DeliveryStatus` owns one private delivery record per review list and row under the configured review root.
`scripts/production/delivery_state.py` owns its typed states and decoder.
Both the page and waiting-count readers use this decoder.
The record has separate validation and verdict slots, each with its latest attempt ID and state.
Its question slot holds the latest question and its answer in the same record.
`NoQuestion`, `OpenQuestion`, and `AnsweredQuestion` state who must act next.
Each new question gets a `question_id` from `secrets.token_hex(4)`.
An open question stores `question_id`, `text`, and `delivery`.
The delivery contains its attempt ID and state or `null` after delivery.
An answered question stores `question_id`, `text`, and `answer`.
The command replaces the open question with the answered question under the delivery lock.
Repeated question saves with the same text never write or send again while that question is open.
A fresh question after an answer gets a new ID.
Before the verdict write, the server stores a pending intent with a new attempt ID.
If this intent write fails, the server refuses the verdict write.
The record stores staff recipient IDs without page context.
The server reads the owner and its production document at use through `StaffRouting.owner_and_production`.
`DeliveryIntent` models a pending send; it has no failed state.
`ValidationRequested` keeps validation ownership and feedback after delivery or an alert.
Only a showrunner answer returns an open question to the user.
The page shows **Saved: <answer>**, **The showrunner will validate this**, or **The showrunner will answer this**.
The page shows no delivery state, attention links, or resend buttons.

The server reads `config/review_links.conf` through `scripts/lib/conf.py` when delivery starts.
Each page render reads `feedback_rows` once to set every feedback box's height.
`delivery_tries` starts at 3; `delivery_pause_seconds` starts at 5.
Tests use a zero pause.
Each round sends once to every recipient that has not succeeded.
The server pauses once between rounds and retries only failed recipients.
The showrunner receives its first attempt before any recipient receives a second attempt.
A `QUEUED:` result with exit 1 counts as kept; a `KEPT:` result with exit 0 also counts as kept.
Other nonzero exits fail.
After all tries fail for a recipient, the server sends one alert to the tab's showrunner, with no alert retries.
The alert names the list, row, recipient, number of tries, and last error.
It states that the user's answer is saved and requests a manual handoff.
If the alert fails, the server prints one line to stderr and stops.
The user receives no delivery alert.

At startup, the server delivers pending records with the same retry and alert rules.
Recovery preserves attempts that a live request owns.
Recovery scans each review list once and finds each record through `change_routing.delivery_path`.
It ignores the `page` field in legacy records.
The browser and recovery use `StaffRouting.owner_and_production` for the same owner and production document.
The `Production:` line names the showrunner in live and recovered messages.
It never delivers a verdict without a saved verdict in the review list.
Legacy failed records decode as nothing to deliver; they produce no send or page controls.
A legacy failed validation keeps validation ownership without a message attempt.
After a send finishes or an alert finishes, the server clears that attempt.
A validation keeps its ownership slot with state `sent`.
An open question keeps its question ID and text; an answered question keeps its answer.
The server deletes the record when every slot is empty.
One stable `fcntl.flock` protects attempt creation and conditional completion.
Sends run outside the lock.
An older attempt cannot clear or change a newer attempt's record.
Each server guards active attempts against duplicate sends.
A request owns its attempt from creation through completion.
Startup recovery skips that request's record under the same lock.
Recovery clears a true orphan verdict intent if the previous process never saved its verdict.

A failed POST keeps its 4xx or 503 status.
The all-list page returns a short JSON reason and keeps the typed answer in the browser.
The browser shows **Not saved: <reason>** and keeps the answer form for another submission.
The server writes full errors only to stderr and the existing showrunner failure message.
Older selected-row forms retain their styled failure page and typed answer.
A fallback form needs only the page manifest and notice when row or queue reads fail.
An unknown page, invalid content type, or refused bind shows one short sentence in the same style.

The browser uses its prepared queue for Skip for now and stores no second queue on the server.
Ask showrunner to check calls the existing queue writer before the response.
One stable lock in `urgency.py` protects pin, selection, and move operations across all queues.
Each operation opens the lock separately and retains atomic file replacement.
A GET never publishes or advances a page.
Older selected-row pages retain their server Skip for now form for compatibility.
The sender's repeat rules also apply when the server recovers a pending message.
The review record remains the only verdict store.

Restart each running review-links service once when this code check reaches deployment.
No NixOS or service configuration change is necessary.

### Server

`serve.py [--port PORT]` reads its default port from `server_port`.
The setting starts at 8765.
`--port` overrides the setting for one server run.
The times below describe the initial config values.
The server reads the current settings when it needs them.

- At start, the server runs `tailscale ip -4` (10 s timeout) and parses the output as IPv4. It creates the root if it is missing and runs the sweep. Then it binds a `ThreadingHTTPServer` to that address and port, and prints `review-links: serving <root> on <address>:<port>`. A failure at start, for example no address or a failed bind, prints one `review-links: <error>` line to stderr, and the server exits 1.
- The server repeats the address check after `tailscale_check_seconds`, which starts at 60 seconds.
  The server uses the monotonic clock. A failed lookup prints `tailscale address gone (was <address>)`. A different address prints `tailscale address changed (was <old>, now <new>)`. In both cases the server exits 1, and the service manager starts it again on the current address. If Tailscale stops but still reports the same address, the server keeps running, because its socket stays bound to that address.
- The server checks loaded module files under its own checkout after `code_check_seconds`, which starts at five seconds.
  It uses a monotonic deadline and reads the interval only when the check is due.
  It compares each file's modification time in nanoseconds and size.
  A file modified at or after the process start counts as changed, even before its first stamp.
  An older module loaded later joins the stamps at the next check without a restart.
  A changed or unreadable file requires a restart.
  The server checks live delivery attempts under the delivery lock.
  It defers the restart while an attempt is live and keeps the old stamps for the next check.
  When no attempt is live, an exception closes the server socket.
  The server prints `review-links: code changed: <path>` and calls `os.execv(sys.executable, sys.orig_argv)`.
  The new process keeps the pid, arguments and environment.
  A failed process replacement or new start exits 1, and the service manager starts the service again.
- The server percent-decodes the path and removes the query. A path with a `..` segment gets 404. The server resolves the path under the root and follows symlinks. The result must be a regular file under the root. Folders and targets outside the root get 404. Index names, the `.review-link-` prefix, and atomic temporary names get 404. The server never lists a folder.
- A published `changes.html` with a private manifest is rendered from the review record. Every other regular file gets 200 with `Content-Type`, `Content-Length`, `Last-Modified` and `X-Content-Type-Options: nosniff`. The server never sends 304.
- Content types: `.html` and `.htm`, in any case, go out as `text/html; charset=utf-8`. A type that Python's `mimetypes` lists under `image/` keeps that type. All other files, `.md`, `.txt`, `.json` and `.pdf` included, go out as `text/plain; charset=utf-8`.
- Each request writes the standard access line to stderr.

### Sweep

`sweep_snapshots(root: Path, now: float) -> int` deletes expired snapshot folders and returns the number deleted.
The server runs it before the bind and after each `snapshot_sweep_seconds` interval.
That interval starts at one hour.

- `snapshot_retention_seconds` sets the expiration age and starts at seven days.
  Each publish resets the folder modification time.
  The sweep deletes expired folders at its next run.
  A stopped server sweeps at its next start.
- At the root, the sweep checks `is_symlink()` before `is_dir()`, so it never follows a symlink. It skips symlinks and entries that are not folders, for example `.index.json`, `.index.lock` and temporary files.
- A root folder is a flat snapshot when its name is six lowercase letters or digits and it holds no subfolder and no symlink. The sweep deletes a flat snapshot as one folder.
- Any other root folder is a session folder of the older `<session>/<UTC stamp>-<label>-<unique suffix>/` layout. The sweep checks each child folder on its own, skips child symlinks and never deletes the session folder.
- An `OSError` on the root listing, on an entry check or on a delete prints `review-links sweep: <path>: <error>` to stderr, and the sweep goes on to the next entry. A folder that is gone before its delete is skipped with no line. The hourly call has one more `OSError` catch around the whole sweep.
- The sweep does not take the index lock and does not change the index. The next publish drops the entries of deleted folders.

### Services

Both machines import `/etc/nixos/modules/common/review-links.nix`. It defines the job `nate.jobs.review-links` with `keepAlive = true` and the script `exec "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/serve.py"`. The job runs the script from the `~/.claude` checkout, not from a Nix store copy, with the user's `PATH`, which has `python3` and `tailscale`. The backends `/etc/nixos/modules/linux/nate-backend.nix` (systemd) and `/etc/nixos/modules/darwin/nate-backend.nix` (launchd) turn the job into the service on each machine. The services set no `REVIEW_LINKS_ROOT` and no `REVIEW_LINKS_HOST`.

- natedev: the systemd user unit `review-links`. The backend sets `Type=simple`, `Restart=always` and `WantedBy=default.target`. `modules/linux/review-links.nix` adds `RestartSec=10s`, `StartLimitIntervalSec=0` and the firewall line `networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8765 ];`. Logs go to the user journal.
- Mac: the launchd user agent `org.nixos.review-links`. The backend makes `launchd.user.agents.review-links` with `KeepAlive = true` and `RunAtLoad = true`. It sets no `Label`, so the nix-darwin prefix `org.nixos` names the agent. There is no darwin-only module. The default launchd `ThrottleInterval` of 10 s is the restart gap. stdout and stderr go to `~/Library/Logs/nate-jobs/review-links.log`.

## Invariants

- Ordinary snapshot content changes only when the same file set is published again without `--new`. Change pages read the change review records at request time. Their publisher writes only a private manifest and placeholder into the snapshot folder; the sweep deletes whole folders.
- A new ID never takes an existing folder. The exclusive `mkdir` refuses a taken ID.
- Each output and the index change by one `os.replace` of a complete temporary file in the same folder. A reader gets the old bytes or the new bytes, never part of a file.
- Every index read, ID pick, snapshot write, cleanup and index write happens under the one `flock` on `<root>/.index.lock`.
- A refused request writes nothing. The input checks and the host lookup run before the publisher creates the root or takes the lock. The one exception is the 10-tries ID refusal: it can leave the root and `.index.lock`, but no snapshot and no index change.
- New snapshot folders hold only regular files. The flat-or-legacy test of the sweep depends on this.
- `atomic_file` names temporary files `.<target>.<pid>.<random>.tmp`. Its `temporary_target` function identifies these names. The server hides every atomic temporary file, including temporary files for public outputs. The publisher refuses snapshot inputs with these names, index names, or the `.review-link-` prefix. For example, `.draft.txt.123.456.tmp` is a reserved snapshot input name.
- The server binds only the IPv4 address from `tailscale ip -4`. `--port` changes only the port. The server exits 1 when that address is gone or changes, and it never serves on an old address.
- The server serves regular files under the resolved root and the selected rows of published change pages, and never lists a folder. It never serves index files, page manifests or unfinished writes, including names in a different letter case.
- One failed sweep entry never stops the bind, the server or the sweep of other entries. The sweep never follows a symlink.
- The configured sweep deletes expired snapshots.
  A session end deletes nothing.
- The skill gives a link only after `curl` on the same machine returns `200`, and only for the review-links service, never for a hand-run server.

## Gotchas

- **Test port.** The publisher uses `server_port`.
  A server with another `--port` is not at the printed link.
- **Mac case-insensitive filesystem.** The publisher compares output names after `str.casefold`, which is full Unicode case folding. `Report.txt` and `report.txt` collide. `Straße.txt` and `STRASSE.txt` collide. `Review.md`, with its `Review.html`, collides with `review.HTML`. This stops one output from overwriting another on the Mac.
- **Private filenames.** `private_name` uses case folding before it checks names. It blocks index names, the `.review-link-` prefix, and atomic temporary names. The server checks names before and after it resolves symlinks. These checks hide page manifests and unfinished writes on both machines.
- **Symlink aliases.** The key uses the resolved path and the published name. A symlink with a different name gets its own ID and its own copy, and the original link stays. A path through a symlinked folder to a file with the same name reuses the ID.
- **The legacy `manual/` folder.** `manual` is six lowercase letters, so it matches the ID pattern. A new snapshot never holds a subfolder or a symlink. So a six-character folder that holds one is a legacy session, and the sweep checks its children one by one. A `manual/` that holds only a symlink keeps it. Empty legacy session folders stay.
- **Links between Markdown files.** A `.md` output goes out as `text/plain`, so a link to `other.md` opens the raw text. Link to `other.html` for the rendered page.
- **Raw HTML.** GitHub renders raw HTML such as `<details>`, and this page shows it as text. Keep a draft for GitHub in plain Markdown, so it looks the same in both places.
- **PDF and other binary files** go out as `text/plain; charset=utf-8` with `nosniff`, so the browser shows them as text.
- **`--new`.** Later runs without `--new` refresh the new folder. The old folder keeps its content until the sweep deletes it, and no run can refresh it again.
- **Atomic per file, not per bundle.** During a refresh of several files, a reader can see one new file next to one old file.
- **A bad index stops all publishing.** If `.index.json` is not valid JSON or is not a JSON object, every run exits 2 until someone repairs or removes the file.
- **`REVIEW_LINKS_ROOT` in a session.** A session that sets it publishes to a folder that the service does not serve, and its links return 404. The `curl` check finds this.
- **Code changes restart the server.** The next scheduled code check detects changes to loaded modules under the checkout and replaces the server process.
  Other publisher changes take effect at the next publisher run.
  These changes need no rebuild.
- **Sweep and refresh at the same moment.** The sweep takes no lock. A refresh of the folder that the sweep deletes at that moment can fail, or can print a link that returns 404. The `curl` check finds this, and a second run gets a new ID.

## Why

- **An IPv4 literal, not a MagicDNS name.** On natedev, `/etc/hosts` maps the short name `natedev` to local addresses, for example `127.0.0.2`, where the server does not listen. So a `curl` check or a browser on natedev fails with that name. A bare `natedev` URL also failed in the user's browser on the Mac, although the Mac resolver returned the Tailscale address of natedev. The literal is the address that the server binds.
- **A short URL.** The URL must not wrap in a terminal. The target is 60 characters. The IPv4 address of each machine has 13 characters, so a 20-character file name gives 53 characters, and a test checks the limit. The full name `natedev.tail21e383.ts.net` gives 65 characters. Percent-encoding makes names longer: a space becomes `%20`, and each non-ASCII letter becomes 6 or more characters.
- **Plain HTTP on the tailnet, no `tailscale serve`.** The server binds only the Tailscale address, so only tailnet devices can reach it. One user service per machine needs no change to the Tailscale configuration. HTTPS, `tailscale serve` and access from outside the tailnet are out of scope.
- **`marked` in the browser.** natedev has no `pandoc` and no Python `markdown` module, so the publisher does not render Markdown itself. The escaped `<pre>` keeps the page readable with no network.
- **Snapshots and change pages.** Ordinary files are copied into the served root and stay after their publishing session ends. Change pages instead show only the review rows authorized by their private manifests. Both expire through the configured sweep.
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


## Configuration

### Code changes

`ReviewLinksServer.service_actions` checks loaded checkout modules through `loaded_module_stamps`.
`code_check_seconds` in `config/review_links.conf` sets the interval, initially five seconds.
Each stamp holds `st_mtime_ns` and `st_size`.
An unreadable file, a file modified at or after process start, or a changed stamp raises `CodeChanged`.
The server prints the changed path, closes its socket, and calls `os.execv(sys.executable, sys.orig_argv)`.
The replacement keeps the pid and environment.
A failed replacement exits for the service manager to restart.
An active verdict delivery postpones the code check without replacing the saved stamps.
The next check can therefore detect the same change.
No merge-time restart command is needed.
The verdict code in `change_review`, `feature_log` and `atomic_file` is loaded into the server, so a change to it also replaces the server process.

### Settings

The scripts read `config/review_links.conf` through `scripts/lib/conf.py` when each operation starts.
`server_port` selects the server port and the port in published URLs.
`tailscale_timeout_seconds` limits address commands.
`snapshot_retention_seconds` controls snapshot age.
`snapshot_sweep_seconds` and `tailscale_check_seconds` control the server checks.
`snapshot_allocation_tries` limits folder allocation attempts.
`delivery_tries` and `delivery_pause_seconds` control message attempts.
`feedback_rows` sets the textarea height from 1 to 20 rows and starts at 5.
`unrouted_alert_recipient` selects the alert recipient and the root showrunner for changes that nobody can take.
This owner rule applies to routed changes when the list has no staff showrunner or its production document is gone.
An unknown producer id or a broken parent chain keeps the change unrouted.
If the list has no document, the unrouted change takes only the root showrunner's document.
If no document is available for the change or the root showrunner, the page shows the unavailable message.
`unrouted_alert_retry_seconds` controls repeated alerts.

The routing scripts read `config/change_review.conf` when they need a folder.
`lists_directory` selects the default change review folder.
`review_links_directory` selects the default delivery folder.
`CHANGE_REVIEW_DIR` overrides the reader folder for tests.
Each publication uses one delivery root for the HTML and its manifest.
Each router resolves the configured alert folder once, when an unsaved unrouted row needs it.
The status line watches `change_review.conf` and `review_links.conf` so folder and root showrunner changes invalidate its counts.
The alert guard uses only the configured folder.
`REVIEW_LINKS_ROOT` overrides the delivery folder.
The page, the footer and `/next` use these same folder readers.
