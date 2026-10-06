# Screen recording skill

> **Status: PLAN — todo.**

> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via the `startup` session, 2026-10-06 04:2x PDT: "Ask natedev session to make a skill for it that others can also use - screen recording. Make sure we have guardrails built in to not accidentally create recordings that run too long. Make sure we have created a cleanup policy so we don't fill our disk with videos."

First consumer: `startup` (Hana's startup-animation video). Reply to it by SendMessage with the skill's name and usage when merged.

## What already works (startup's findings, natedev, KDE Plasma on Wayland)

- ffmpeg has no PipeWire input, so it cannot grab a native Wayland window. An app launched with `env -u WAYLAND_DISPLAY` opens an Xwayland window that `x11grab` can record.
- Window id: `xprop -root _NET_CLIENT_LIST`, then `xprop -id <id> WM_NAME`. Hana debug titles contain `debug <port>`. xwininfo, xdotool and wmctrl are not installed.
- `ffmpeg -f x11grab -framerate 60 -window_id <id> -i $DISPLAY -t <secs> -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p out.mp4`: 7 s at 1280×720, 60 fps = 330 KB, real speed.
- Working prototype: `/tmp/claude-1000/-home-natepiano-rust-tool-based-ui-startup-polish/556caf6e-713d-47c0-a8fc-5dd5e8c26ce4/scratchpad/p14_record.sh` (launch Hana on a test port, find window, record, shut down over BRP `brp_extras/shutdown`). Memory: `~/.claude/projects/-home-natepiano-rust-hana/memory/record-hana-video-via-xwayland.md`.
- Clip check without watching: per-frame `signalstats` `lavfi.signalstats.YAVG`; for a design review, extract stills for a fresh helper.
- Bevy 0.19 `EasyScreenRecordPlugin` is not a substitute (needs libx264 neither machine has, toggles on Space, steps `Time<Virtual>`).

## Decisions (showrunner)

- **Name:** `/screen_record` (`commands/screen_record.md`), script under `scripts/screen_record/`.
- **Linux only now.** The Mac (`screencapture -v` / avfoundation) waits for a real consumer there.
- **Records an existing window by title substring.** Launching and shutting down the app stays with the caller; the skill doc gives the Hana recipe (Xwayland launch, BRP shutdown after).
- **Length guardrails:** the duration is required, no default; a hard ceiling of 60 s with no override flag; an outer `timeout` (duration + 15 s) kills ffmpeg if `-t` fails; `-fs` caps the file at 500 MB. A refusal names the limit it hit.
- **Cleanup:** one directory, `~/.cache/screen-record/`. Every run first prunes clips older than 7 days, then the oldest until the directory is under 2 GiB. The skill doc tells callers to delete their clips once sent or reviewed. Check it against the disk-floor sweeper (500 GiB floor on `/`) and say in the as-built how the two relate.

## Phase 1: `/screen_record` on Linux

The script, its tests (guardrail refusals, pruning order and size cap, timeout kill), the command doc, and a live check: record a few seconds of any Xwayland window and confirm the clip's length and frame brightness. No Hana launch needed; startup records Hana.
