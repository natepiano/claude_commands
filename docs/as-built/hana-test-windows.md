# Hana test windows

Every agent Hana launch names the feature under test in `HANA_TEST_PURPOSE`
and its unit director in `HANA_TEST_OWNER`. The owner defaults to the caller's
unit name in the staff record: the caller's own row for a unit, its parent unit
for a worker. No row gives an empty owner. `hana_shot.py launch` and captures with `--launch` take
`--purpose` and `--owner`. A launch without a non-empty purpose refuses before
starting a process. `mac_run.sh` and `smoke_launch.sh` take the same labels
from the caller's environment. The Mac command keeps both labels when it
starts from Terminal.

Test clones on either machine use `copies.py clone <source> --task gate:<id>`
(or a real `phase:<unit>/<id>` or `--until <when>`). The command records the
task before Git starts and prints the checkout inside its scratch folder.
`scratch.py end --task gate:<id>` removes the clone. A failed clone remains
owned by its task, so the same end command removes a partial copy.
`mac_run.sh` uses one gate task per run. On exit, `hana-instances stop-checkout <path>`
stops its test Hanas through the identity-checked stop path, confirms they
exited, and checks for surviving builds and examples through the audit's
process-use scan. Only then does the script end the task. A failed stop,
unreadable inventory or failed SSH keeps the checkout and names the cause.
It keeps no standing test clone. Purpose and owner reach the Terminal command.

The folder audit also checks registered test clones in the scratch root and
loose Git clones. `clone_max_hours` in `hana_instances.conf` sets their idle limit;
other loose folders keep their existing limits. On the Mac, an audit below
`mac_test.conf`'s `free_floor_gib` removes idle clones oldest first until the
floor is met. A clone with unsaved work keeps its source and loses only its
`target`. Holds, busy locks, live process paths and sockets still keep a clone.
An unavailable process scan keeps it too, including a Hana whose environment
cannot be read. No Hana is stopped to reclaim a busy clone. The audit's arm
and dry-run controls apply to both age and floor removal.
Discovery resolves aliases such as `/tmp` before checking process paths and
holds. Old standing test clones have the same age and floor rules. A pending
removal retries after all live-use guards pass, even if deletion already
removed part of Git's metadata.

The BRP launch hook checks the Hana target's `env.HANA_TEST_PURPOSE` before
the hold and memory checks. An empty or missing purpose denies the launch
and names the key to add. Other targets use the existing memory gate.

`scripts/hana_instances/hana_instances.py list` reads natedev's process table and the Mac's
`ps eww` output over SSH. On macOS it reads the local process table with
`ps`, without `/proc`. Darwin environment reads use herdr's stable command
and environment boundary. It identifies executables at `target/<profile>/hana`
and prints machine, pid, BRP port, owner, purpose, age and checkout. Missing
labels print as `unlabelled`. An unreachable Mac prints one line and leaves
natedev's rows visible. Mac host and SSH options come from `mac_test.conf`.
An environment read failure keeps the process in the list with unknown labels
and reports the cause. Stop refuses that row, and sweep leaves it alone while
it handles other processes and returns a failure status.

`hana-instances stop <machine>:<pid|port>` stops a matching test Hana. An already-gone
process succeeds. `hana-instances sweep --dry-run` lists test Hanas over their limits;
`hana-instances sweep` sends TERM and then KILL after the configured grace period if
the process remains. Collection saves the process start identity. Each TERM
and KILL checks that identity again, so a reused PID receives no signal. A
stop prints success only after the saved process is gone. Sweep reports each
failed stop, continues with the other processes, and exits non-zero if any
stop failed. No owner-session record is
needed: the process carries its own labels.

`config/hana_instances.conf` holds the exempt ports, labelled lifetime, unlabelled
lifetime and stop grace. Each invocation reads those values through
`scripts/lib/conf.py`. `CONF_DIR` selects the configuration folder.
A Hana is a test when it has either test label.
A non-exempt port also identifies a test.
The environment markers `CLAUDECODE`, `SSH_CONNECTION`, `CLAUDE_CODE_SESSION_ID`
and `CODEX_THREAD_ID` also identify tests. A Hana on an exempt port with none of
these markers is a user Hana, and neither stop nor sweep signals it. A test with neither label
uses the shorter unlabelled limit.

Tests inject process files, environment reads, SSH output and signals. They
never signal a real process or SSH to the Mac. Installation of the `hana-instances`
wrapper and the scheduled sweep belongs to the director after checkpoint.

Capture limits come from `config/hana_shot.conf` through the same reader.
The `window_frames` key sets the frame count after a window change.
They include BRP deadlines, capture deadlines, camera waits, output limits, process waits, and worker count.
The reader uses `CONF_DIR` for private test settings.
`scratch.conf` controls the clone folder lifetime, visible root, hold file, and repository scan.
The folder audit reads its other limits from `lint.conf` through the same reader.
All moved values retain their previous defaults.

`mac_run.sh` and `smoke_launch.sh` read their launch settings from `production.conf`.
Smoke launches share `hana_shot.conf` `host` and `refused_port` with Hana commands.
Mac runs share `mac_test.conf` `connect_timeout_s` and round it up for SSH.
SFTP receives the configured bundle path unchanged. SSH receives shell quoting for the same path.
These settings include ports, paths, wait times, and the Mac demo list.
The Mac script calls `conf.py`. The smoke script uses the shared `conf.sh` reader.
