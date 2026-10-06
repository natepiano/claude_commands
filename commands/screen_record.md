---
description: Record an existing X11 window on Linux to a short MP4 clip (1 to 60 s, required); every run prunes old clips. Args - '<window title substring>' <seconds> [name]
---

1. `$ARGUMENTS` is `'<window title substring>' <seconds> [name]`; quote a substring that holds spaces. This command runs on Linux (natedev). The Mac (`screencapture -v` or avfoundation) waits for a real consumer there.
2. Launch the app before recording and shut it down afterward. The window must exist or appear within 10 s. On natedev's Wayland session, launch it as an X11 client with `env -u WAYLAND_DISPLAY`, then start the recorder right after launch.
3. Run `python3 ~/.claude/scripts/screen_record/screen_record.py --window '<substring>' --seconds <N> [--name <label>]`. Stdout contains the clip path on one line when a clip is kept. Exit 0 means recorded; 1 means it could not record; 2 means the request was refused; 3 means a limit stopped the recording.
4. Duration is required with no default. The ceiling is 60 s with no override; ask the user when a task needs longer. The outer timeout stops ffmpeg at duration + 15 s, and the file is capped at 500 MB. A refusal or stop names the limit it reached.
5. For Hana, choose a port for this launch, never the user's 15702. Build the binary first so launch does not compile. Its debug window title contains `debug <port>`. Run this as one Bash call:

   ```bash
   env -u WAYLAND_DISPLAY BRP_EXTRAS_PORT=<port> RUST_LOG=info <worktree>/target/debug/hana > <scratchpad>/hana.log 2>&1 &
   pid=$!
   clip=$(python3 ~/.claude/scripts/screen_record/screen_record.py --window "debug <port>" --seconds 7 --name startup)
   curl -s -m 10 -X POST http://localhost:<port> -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"brp_extras/shutdown"}' > /dev/null
   wait "$pid"
   echo "$clip"
   ```

6. Check a clip without watching it: `ffprobe -v error -show_entries format=duration -of csv=p=0 <clip>` reports its length. `ffmpeg -v error -i <clip> -vf signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=- -f null -` reports per-frame brightness (16 is black). For a design review, extract stills with `ffmpeg -v error -i <clip> -vf fps=2 <scratchpad>/frame-%03d.png` into your own scratchpad, never the clip directory, and give them to a fresh helper to judge. Bevy 0.19's `EasyScreenRecordPlugin` is no substitute: it needs libx264 in the app, toggles on Space, and steps `Time<Virtual>`.
7. Clips live only in `~/.cache/screen-record/`. Each run first prunes clips older than 7 days, then the oldest until the directory is under 2 GiB. Delete your clip (`rm <path>`) as soon as it is sent or reviewed. The disk-floor job does not sweep this directory.
