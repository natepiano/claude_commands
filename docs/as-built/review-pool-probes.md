# Review pool mesh probes

The pool no longer takes reviews. Unit directors use `assign.sh --role review`.
The code and historical procedures below remain until the next phase removes them.

Run on 2026-10-12, Codex CLI 0.162.1, from this worktree. All model turns used
`gpt-6.1-sol`, effort `low`, and the inherited default service tier. The initial
run used nine explicit model turns and one compaction. The repair rerun used
seven explicit model turns and one compaction across three servers. Sections
1–2 report the repaired paths; sections 3–5 retain the initial measurements.
Both instructions files were 7,168 UTF-8 bytes; their first rule was
“End every final reply with exactly PROBE_OK.”

The command lines below use these abbreviations for the actual CLI arguments:

```sh
D=/tmp/claude/delegate/review-pool-probe
mesh() { scripts/lib/py scripts/agents/codex_mesh.py "$@"; }
follow() {
  mesh follow --session-dir "$D" --to "$1" --role review \
    --model gpt-6.1-sol --effort low --message-file "$D/prompt.txt" \
    --summary-file "$D/reply.txt" --log-file "$D/turn.log" --timeout 180 "${@:2}"
}
mesh prime --session-dir "$D" --name template \
  --instructions-file "$D/instructions.txt" --cwd "$D/cwd-a" --model gpt-6.1-sol
```

Tokens came from each thread's rollout, using the fields
`scripts/usage/rollouts.py` (`token_usage`) reads: uncached input = input minus
cached input.
Single-request measurements use `last_token_usage`; turns with tool calls use
`total_token_usage` from a fork with no inherited turns. After revert, the repair
probe used the active rollout path returned by `thread/read`, since revert
created another rollout for the same thread id. The cost comparison uses
the repository's `gpt-6.1-sol` weights (`scripts/usage/weights.py`, in force
from 2026-10-10T21:35Z): uncached input 1, cached input 0.05, output 5.

## 1. First request after reset versus fork

```sh
mesh fork --session-dir "$D" --from template --name lease-reset
follow lease-reset --read-only           # warmup: “Reply in one line: ready.”
follow lease-reset --read-only           # second follow, same prompt
mesh reset --session-dir "$D" --to lease-reset
follow lease-reset --read-only           # same prompt
mesh fork --session-dir "$D" --from template --name lease-fork
follow lease-fork --read-only            # same prompt
```

| Request | Uncached input | Cached input | Output | Weighted cost | Follow seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Warmup | 12,372 | 11,392 | 8 | 12,981.6 | 8.136 |
| First after reset | 212 | 23,552 | 9 | 1,434.6 | 6.935 |
| First after fork | 12,372 | 11,392 | 8 | 12,981.6 | 8.259 |

Prime took 2.007 s; the warmup fork took 0.670 s. Reset itself took 0.199 s;
the comparison fork took 0.189 s. **Reset was cheaper for lease end with the
instructions retained.** Every reply ended with `PROBE_OK`. The repair follows
all used `--read-only`; compare reset and fork within this run, rather than
treating the earlier and repaired requests as identical. One sample does not
establish future cache behavior.

Before the repair — warmup: uncached 11,115, cached 11,392, output 6, weighted 11,714.6, follow 6.740 s, reply `ready.`.
Before the repair — first after reset: uncached 235, cached 22,272, output 6, weighted 1,378.6, follow 7.877 s, reply `ready.`.
Before the repair — first after fork: uncached 15,339, cached 7,168, output 6, weighted 15,727.4, follow 6.524 s, reply `ready.`.
Before the repair — reset took 0.206 s and fork 0.181 s; neither measured reply obeyed the closing rule or retained it in its rollout.

## 2. Developer instructions through the thread lifecycle

```sh
# Continue section 1 on lease-reset, after its reset-and-follow:
mesh compact --session-dir "$D" --to lease-reset
follow lease-reset --read-only
mesh stop --session-dir "$D"
mesh serve --session-dir "$D"
reopen_scratch lease-reset lease-reopened
follow lease-reopened --read-only
mesh reset --session-dir "$D" --to lease-reopened
mesh stop --session-dir "$D"
mesh serve --session-dir "$D"
reopen_scratch lease-reopened lease-reset-reopened
# Client: resume with _resume_params, then thread/turns/list -> data: []
follow lease-reset-reopened --read-only
```

`stop` deliberately marks roster names `ended`. The restart probes registered
fresh scratch aliases of the **same persisted thread id**, retaining the
instructions path, rather than changing this terminal behavior. The alias setup
used the worktree's helpers; this shell abbreviation shows its arguments:

```sh
reopen_scratch() {
  scripts/lib/py - "$D" "$1" "$2" <<'PY'
import sys
from scripts.agents import codex_mesh as mesh
root, source, target = sys.argv[1:]
record = dict(mesh._lookup(root, source))
record["status"] = "done"
mesh._update_roster(root, target, record)
PY
}
```

| Transition | Uncached input | Cached input | Output | Input total | Follow seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| First follow after fork | 12,372 | 11,392 | 8 | 23,764 | 8.136 |
| Second follow | 233 | 23,552 | 8 | 23,785 | 8.147 |
| Follow after reset | 212 | 23,552 | 9 | 23,764 | 6.935 |
| Follow after compact | 12,789 | 11,392 | 9 | 24,181 | 6.013 |
| Follow after stop and serve | 139 | 24,064 | 9 | 24,203 | 6.195 |
| Follow after reset, stop and serve | 212 | 23,552 | 8 | 23,764 | 6.758 |

**The closing rule was obeyed on all six transitions:** each reply was
`ready PROBE_OK` or `ready. PROBE_OK`. The user prompt contained no closing rule.
Supplying the file text as `developerInstructions` on resume and fork restored
the standing contract. Direct same-name follow after `stop` remains refused;
the restart rows measure disk resume of the same conversation through aliases.

Compact succeeded in 12.307 s and produced one compaction record. The
compaction turn reported 0 input tokens, so `compact` stores the size as
`not_measured_since_compaction` and prints `compacted <name>: 23764 tokens,
size not measured until its next turn`. Its last input/cached/output counters were zero with
`total_tokens=19622`; those counters cannot establish compaction billing cost.
The final reset took 0.195 s. After the following restart, `thread/turns/list`
returned zero turns, and the next request's 23,764 input tokens equalled the
fresh fork's zero-turn size. The reverted history stayed gone across restart.

Before the repair — direct first turn: uncached 13,133, cached 12,288, output 9, reply `ready. PROBE_OK`, rollout duration 4.689 s; its unsubscribed observer reached a 180 s deadline.
Before the repair — first follow after compact: uncached 11,869, cached 12,288, output 6, reply `ready.`, follow 5.427 s; the closing rule was lost.
Before the repair — compact succeeded in 4.740 s with one compaction record; input/cached/output counters were zero with `total_tokens=18086`, so billing cost was unavailable.

## 3. Per-turn cwd and read-only policy

```sh
mesh fork --session-dir "$D" --from template --name cwd-check
# cwd-b/known.txt contains SECOND_WORKTREE_SENTINEL; cwd-a has no known.txt.
follow cwd-check --cwd "$D/cwd-b" --read-only
```

This turn used separate `cwd-prompt.txt`, `cwd-reply.txt`, and `cwd-turn.log`
files. The prompt asked it to read `known.txt`, attempt
`printf WRITE_PROBE > blocked.txt`, and report the result in one line.
It returned `SECOND_WORKTREE_SENTINEL`, write exit 1, and no `blocked.txt`.
The recorded shell output said `read-only file system: blocked.txt`; an external
file check also found no file. The rollout's turn context named `cwd-b` and
`sandbox_policy: {"type":"read-only"}`.

**Both flags held in the second scratch directory.** It was an isolated
directory representing a second worktree, without creating a Git worktree.
The turn took 14.015 s and used 22,901 uncached input, 22,400 cached input, and
141 output tokens across its two model requests.

## 4. Two follow calls on one server

```sh
mesh fork --session-dir "$D" --from template --name parallel-a
mesh fork --session-dir "$D" --from template --name parallel-b
follow parallel-a &
follow parallel-b &
wait
```

Each process used its own `parallel-<worker>-prompt.txt`, reply, and log file.
Both prompts asked for `sleep 4`, then a one-line `parallel complete.` reply.
One observation after launch read both threads as `active` on server port
49161. Both turns started at 19:07:31 UTC.

| Thread | Turn duration ms | Completed UTC | Uncached input | Cached input | Output |
| --- | ---: | --- | ---: | ---: | ---: |
| parallel-a | 10,074 | 19:07:41 | 22,718 | 22,400 | 54 |
| parallel-b | 29,248 | 19:08:00 | 22,715 | 22,400 | 51 |

**Two follow turns ran concurrently on one server**, with about 10 s of overlap
and successful replies from both. This establishes overlap, not equal latency.

## 5. Unloaded template and 7 KB instructions

```sh
mesh stop --session-dir "$D"
mesh serve --session-dir "$D"
# Client: thread/loaded/list {} -> {data: [], nextCursor: null}
mesh fork --session-dir "$D" --from template --name cold-fork
follow cold-fork
mesh reset --session-dir "$D" --to cold-fork
mesh reset --session-dir "$D" --to cold-fork  # now has no turns
mesh stop --session-dir "$D"
```

The first server was PID 256528, port 49161; the new server was PID 427717,
port 42279. Its loaded-thread count before fork was zero. **Fork succeeded
against the unloaded template** in 0.352 s; follow succeeded in 7.383 s, using
11,115 uncached input, 11,392 cached input, and 6 output tokens. Final-code
reset succeeded both with a turn (0.616 s) and without turns (0.132 s).

**The 7,168-byte instructions file was accepted** by prime in 0.998 s without
a model turn; the direct first turn in probe 2 obeyed its rule. Acceptance did
not establish persistence through resume/fork.

Both initial scratch servers were stopped. The final PID no longer existed and its
websocket refused connection. Turn logs/replies were read and deleted after
each probe; the eight probe rollouts were removed after their final use, and
the scratch session directory was deleted. No scratch app-server remains.

The repair servers were PID 1010624/port 38043, PID 1154513/port 51775, and
PID 1193625/port 59611. Each stopped PID no longer existed. The repair session
and its five rollouts, including the two created by revert, were deleted;
logs and replies were read and deleted before subsequent turns.

**Reset gives the cheaper first request with instructions retained: 1,434.6 versus 12,981.6 weighted tokens in this measured pair.**
