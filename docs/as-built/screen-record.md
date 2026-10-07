# Screen recording (`/screen_record`)

## What it is

`/screen_record` records one existing X11 window on Linux to a short MP4 clip, so an agent can capture what an app does on screen (Hana's startup animation was the first use) and check or send the result without anyone watching it live. The window is chosen by a substring of its title. Guardrails keep a recording from running long: the duration is required, capped at 60 s with no override, bounded again by an outer `timeout`, and the file is capped at 500 MB. A cleanup policy keeps clips from filling the disk: clips live in one directory, and every run first deletes clips older than 7 days, then the oldest until the directory is under 2 GiB. The recorder only records. Launching the app before and shutting it down after stay with the caller.

## How it works

### Files

| File | Role |
| --- | --- |
| `commands/screen_record.md` | The `/screen_record` skill: arguments, the caller's launch and shutdown duties, the Hana recipe, how to check a clip, cleanup duties. |
| `scripts/screen_record/screen_record.py` | The recorder: refusals, tool and display checks, prune, window lookup, recording under `timeout`, outcomes. Python 3.13 standard library only. |
| `scripts/screen_record/test_screen_record.py` | `unittest` cases that run the script as a subprocess with `xprop`, `timeout` and `ffmpeg` stubs alone on `PATH` and a temporary `HOME`. |
| `~/.cache/screen-record/` | The clip directory, outside the repository (mode 0700). |

### Command line

```
python3 ~/.claude/scripts/screen_record/screen_record.py --window <title substring> --seconds <N> [--name <label>]
```

- `--seconds` is required with no default: a whole number from 1 to 60. Leading zeros are accepted (`007` is 7).
- `--window` is a non-empty, case-sensitive substring of the window's `WM_NAME`.
- `--name` defaults to `clip` and must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Arguments are parsed into `ScreenRecordArguments(argparse.Namespace)`, the typed pattern that keeps basedpyright free of `Any`.

The skill takes `'<window title substring>' <seconds> [name]` and runs the line above. It names the script by its live path under `~/.claude`.

### One run

```
main()
  refuse (exit 2) on: not Linux, --seconds missing / not 1–60, empty --window, bad --name
  missing xprop / timeout / ffmpeg (named together)      → exit 1
  DISPLAY unset                                          → exit 1
  directory = $SCREEN_RECORD_DIR (when non-empty) or ~/.cache/screen-record; mkdir 0700
  prune(directory)
  find_window(substring) → FoundWindow(window_id, title)  or WindowLookupFailure → exit 1
  clip_path(directory, name) → reserved path
  record(window_id, title, seconds, display, path)       → exit 0, 1 or 3
```

Every external tool runs from an argument list, never through a shell. Stdout carries one line, the clip's absolute path, and only when a clip is kept. Everything else goes to stderr, prefixed `screen_record:`.

### Prune

`prune(directory)` runs before the window lookup:

1. Collect the regular `*.mp4` files directly in the directory. Symlinks, subdirectories and other names are neither counted nor deleted.
2. Delete each one whose mtime is more than 7 days old (`MAX_AGE_SECONDS = 604_800`).
3. Sort the rest oldest first (mtime, then name) and delete until the total is under 2 GiB (`DIRECTORY_LIMIT = 2_147_483_648`).
4. When anything went, print `pruned <n> clips (<MB> MB): <a> older than 7 days, <b> to bring the directory under 2 GiB`.

A file that cannot be inspected or deleted is reported on stderr and skipped. The run goes on.

### Window lookup

`find_window(substring) -> FoundWindow` uses `xprop` alone, since xwininfo, xdotool and wmctrl are not installed on natedev:

- `xprop -root _NET_CLIENT_LIST` lists window ids (hex), then `xprop -id <id> WM_NAME` reads each title. `STRING` and `UTF8_STRING` titles are read; escaped quotes and backslashes are unescaped. A window without a title is skipped.
- The whole lookup has 10 s. It polls every 25 ms, and each `xprop` call gets a subprocess timeout equal to the time left.
- Exactly one match returns. Several matches fail at once with `<n> windows match '<s>': <titles>; pass a longer substring`. No match after 10 s fails with `no window title contains '<s>' after 10 s; titles seen: <titles>`. A failing `xprop -root` fails at once with its stderr.

### Recording

`clip_path(directory, label)` names `<label>-<UTC YYYYmmddTHHMMSSZ>.mp4`, adding `-2`, `-3`, … on a collision, and reserves the name with `os.open(O_CREAT | O_EXCL | O_WRONLY, 0o600)` before ffmpeg starts. `record()` then runs:

```
timeout --kill-after=5 <seconds + 15> \
  ffmpeg -hide_banner -loglevel error -nostdin -y \
    -f x11grab -framerate 60 -window_id <decimal id> -i $DISPLAY \
    -t <seconds> -fs 500000000 \
    -vf crop=trunc(iw/2)*2:trunc(ih/2)*2 \
    -c:v libx264 -preset veryfast -tune zerolatency -crf 18 -pix_fmt yuv420p <path>
```

`-y` overwrites the reserved empty file. The crop trims an odd width or height to even, which libx264's yuv420p requires; a 401×301 window records as 400×300. `-tune zerolatency` keeps x264 from queueing frames, so ffmpeg exits soon after `-t` (see Calibration). Outcomes:

| Exit | Cause | Clip | Stderr |
| --- | --- | --- | --- |
| 0 | ffmpeg exited 0, clip under 500 MB | kept, path printed | `recorded <s> s of '<title>' (<KB> KB)` |
| 3 | `timeout` returned 124 or 137, or was killed by SIGKILL (−9) | deleted, stdout empty | `stopped by the outer timeout at <d> s (<s> s + 15 s): ffmpeg did not stop at -t; partial clip deleted` |
| 3 | ffmpeg exited 0, clip at or over 500,000,000 bytes | kept, path printed | `stopped at the 500 MB file cap (ffmpeg -fs); the clip is shorter than <s> s` |
| 1 | any other nonzero exit, unreadable clip, `OSError` | deleted | `ffmpeg exited <rc>; partial clip deleted`, or `could not record: <error>` |
| 2 | refused before anything ran | none | one line naming the limit, values shown with `repr` |

Every failure deletes only the path this run reserved.

### The skill doc

`commands/screen_record.md` tells the caller to:

- Launch the app first, as an X11 client with `env -u WAYLAND_DISPLAY` on natedev's Wayland session, and start the recorder right after, since the window must appear within 10 s.
- For Hana: build the binary first so launch does not compile, pick a port per launch (never the user's 15702), match the title `debug <port>`, and shut it down afterward with a BRP `brp_extras/shutdown` call. The doc gives the whole sequence as one Bash call.
- Check a clip without watching it: `ffprobe … format=duration` for its length, `signalstats` `lavfi.signalstats.YAVG` per frame for brightness (16 is black). Stills for a design review go to the caller's own scratchpad, never the clip directory, and a fresh helper judges them.
- Delete the clip (`rm <path>`) as soon as it is sent or reviewed.

### Disk: the prune and the disk-floor job

The clip directory is on `/`, the same filesystem the disk-floor job (`/etc/nixos/modules/linux/disk-floor.nix`) holds at 300 GiB free. That job runs `scripts/lint/sweep.py --floor-only` every 2 minutes and frees only cargo target directories, then sends a phone alert when `/` is under 150 GiB. It never touches `~/.cache/screen-record/`. The prune is that directory's only bound: under 2 GiB when a run starts, plus the one clip that run writes, at most a little over 500 MB. Clips can therefore take at most about 2.5 GB of the room the floor protects, and when `/` drops under the floor the sweep deletes build caches, never clips.

## Invariants

- No flag, environment variable or setting raises the 60 s ceiling, the 15 s timeout margin or the 500 MB file cap. A refusal or a stop names the limit it reached.
- A refusal (exit 2) has no side effects: no directory created, nothing pruned, no tool run.
- Stdout holds only the kept clip's path, on one line; callers capture it with `clip=$(…)`.
- A failure deletes only the path its own run reserved, never another clip.
- The prune touches only regular `*.mp4` files directly in the clip directory, and runs before the window lookup.
- Tools run from argument lists, never a shell.
- Linux only: on any other platform the script refuses with exit 2.
- Tests never write the real `~/.cache/screen-record` (they set `SCREEN_RECORD_DIR` and `HOME` to a temporary directory), never run the real `ffmpeg` or `xprop` (stubs on a `PATH` that holds only the stub directory), and never launch an app or open a window.
- `pyrightconfig.json` gives `scripts/screen_record` no sibling-import path, so the tests run the script as a subprocess and never import it.
- Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings.

## Calibration and gotchas

- **Wayland.** ffmpeg has no PipeWire input, so a native Wayland window cannot be recorded. Launch the app with `env -u WAYLAND_DISPLAY` so it opens under Xwayland (`DISPLAY=:0` on natedev). An ffplay test window also needs `SDL_VIDEODRIVER=x11`, or SDL opens a native Wayland window that is absent from Xwayland's client list.
- **Clip size.** 7 s of a 1280×720 window at 60 fps came to 330 KB, recorded at real speed. A busy window costs far more, but a 60 s clip rarely reaches the 500 MB cap.
- **The file cap.** ffmpeg exits 0 when `-fs` stops it, and the file ends slightly over the cap. The script tells a capped clip from a full one by its size alone (`>= MAX_BYTES`).
- **The outer timeout's kill path.** `timeout` returns 124 when ffmpeg stops on the TERM. If ffmpeg ignores the TERM for 5 s, `--kill-after` sends SIGKILL to `timeout`'s whole process group, `timeout` itself included, so Python reads a return code of −9, not 137. `screen_record.py` treats −9 like 124 and 137: exit 3, clip deleted. A test drives the real `timeout` against a stub ffmpeg that ignores TERM and checks the run matches the 137 case.
- **Odd window sizes.** libx264 refuses yuv420p at an odd width or height (`width not divisible by 2 (401x301)`, ffmpeg exit 187). The crop trims one column or row: 401×301 records as 400×300.
- **Overrun after `-t`.** `-t` ends the capture on time, but x264's default lookahead and frame threads hold dozens of frames, and ffmpeg encodes all of them before it exits. On a 3840×2012 window at load average 196, default veryfast had written 0 of 53 frames at the `-t` cutoff and exited 1.56 s later; with `-tune zerolatency` it had written 103 of 112 and exited 0.26 s later. Starved to half a core (`systemd-run --user --scope -p CPUQuota=50%`), the old command hit the outer timeout on a 10 s clip (exit 3 at 31 s); with zerolatency it finished in 12.9 s. FFmpeg 8.1's default fps mode adds no duplicate frames here (`dup=0`), so `-fps_mode` changes nothing.
- **Prune timing.** The prune runs only when a recording run gets past its refusals and its tool and `DISPLAY` checks. Nothing expires clips between runs, so the 7-day limit takes effect at the next run, and the caller's `rm` is what keeps the directory empty.
- **Lookup time.** A failed lookup takes the full 10 s; the no-match test waits it by design. A substring that matches more than one title fails at once; pass a longer one.
- **Window id.** `xprop` reports hex ids; ffmpeg gets the decimal form.
- **Non-clip files.** Files that are not `*.mp4` are not counted toward 2 GiB and never pruned, which is why stills go to the caller's scratchpad.

## Why

- **Required duration, hard ceiling, two more bounds.** A default duration would let a forgotten argument record anyway, and an override flag would let a recording run long. `-t` stops a normal run; the outer `timeout` stops ffmpeg when `-t` fails; `-fs` bounds the size whatever the duration.
- **Prune at the start of every run.** The cleanup needs no separate job: whoever records next leaves the directory under its bound. Age goes first, so a clip within 7 days is deleted only for size.
- **The recorder prunes for itself.** The disk-floor job frees only cargo target directories, so without the prune nothing would bound the clip directory.
- **Records an existing window; the caller launches and shuts down.** Each app launches and stops its own way (Hana under Xwayland on a chosen port, stopped over BRP), so the recorder stays generic and the skill doc carries the Hana recipe.
- **`O_EXCL` reservation, not `-n` with an `exists()` check.** Two runs with one label in one second could otherwise each write and then delete the other's clip.
- **Not Bevy 0.19's `EasyScreenRecordPlugin`.** It needs libx264 in the app, toggles on Space, and steps `Time<Virtual>`.
- **Linux only.** The Mac (`screencapture -v` or avfoundation) waits for a real consumer there.
