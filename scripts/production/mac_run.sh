#!/usr/bin/env zsh
# Run a sha on the Mac: ship it from this machine, build hana, run it, check it
# stays up for SECONDS, then stop it. User, 2026-10-04: after each green CI on
# the merge branch, when the Mac is reachable.
#
# Then it builds each demo example with the same cargo arguments you would
# type, so `cargo run` in the clone starts at once, and runs it for
# EXAMPLE_HOLD seconds. User, 2026-10-05: the Ian Hubert demo runs from this clone.
#
# Usage: mac_run.sh <repo dir> <sha> [seconds; production.conf mac_run_hold_seconds sets the default]
# Exit: 0 all stayed up; 3 Mac unreachable; 5 wrong tree; 6 build failed;
# 7 hana exited early; 8 hana stayed up but an example failed; 9 Mac blocked,
# busy with a test, or coordination state unreadable.
set -o pipefail
purpose=${HANA_TEST_PURPOSE:-}
owner=${HANA_TEST_OWNER:-${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}}
[[ -n ${purpose//[[:space:]]/} ]] || { print -u2 'mac_run: set HANA_TEST_PURPOSE to the feature under test'; exit 2 }
# zsh's q flag quotes labels for the generated command without evaluating them.
purpose_arg=${(q)purpose}
owner_arg=${(q)owner}
lib=${0:A:h}/../lib
source "$lib/conf.sh"
repo=$1 sha=$2
hold=${3:-$(conf_value production mac_run_hold_seconds)} || exit 2
host=$(conf_value production mac_run_host) || exit 2
# Each run owns a scratch task; EXIT ends it after the test processes finish.
clone=""
clone_arg=""
clone_task=""
port=$(conf_value production mac_run_port) || exit 2
log=$(conf_value production mac_run_log) || exit 2
launch=$(conf_value production mac_run_launch) || exit 2
status_file=$(conf_value production mac_run_status) || exit 2
bundle=""
example_hold=$(conf_value production mac_run_example_seconds) || exit 2
# Each entry names a crate, an example, and its optional feature.
example_settings=$(conf_value production mac_run_examples) || exit 2
examples=("${(@s:;:)example_settings}")
connect_seconds=$(conf_value mac_test connect_timeout_s) || exit 2
# SSH requires a whole timeout; retain the configured minimum.
integer connect_whole=$connect_seconds
connect_seconds=$(( connect_whole + (connect_seconds > connect_whole) ))
claim_seconds=$(conf_value production mac_run_claim_seconds) || exit 2
deployment_target=$(conf_value production mac_run_deployment_target) || exit 2
status_grace=$(conf_value production mac_run_status_grace_seconds) || exit 2
poll_seconds=$(conf_value production mac_run_poll_seconds) || exit 2
log_lines=$(conf_value production mac_run_log_lines) || exit 2
example_log_lines=$(conf_value production mac_run_example_log_lines) || exit 2
remote_bundle=$(conf_value production mac_run_bundle) || exit 2
example_log_prefix=$(conf_value production mac_run_example_log_prefix) || exit 2
# Quote each configured path before it enters a remote shell command.
log_arg=${(q)log}
launch_arg=${(q)launch}
status_arg=${(q)status_file}
bundle_arg=${(q)remote_bundle}
source_repo=$(conf_value production mac_run_source) || exit 2
source_arg=${(q)source_repo}
[[ $source_repo != \~/* ]] || source_arg="\$HOME/${(q)${source_repo#\~/}}"

# Tailscale SSH serves the Mac, and its exit status is always 0, so a command
# whose status matters prints it as its last line and on_mac returns that.
on_mac() {
  local out code
  out=$(ssh $host "$1; echo mac_exit=\$?") || return $?
  print -rn -- "${out%mac_exit=*}"
  code=${out##*mac_exit=}
  [[ $code == <-> ]] || code=255
  return $code
}

ssh -o ConnectTimeout=$connect_seconds -o BatchMode=yes $host true 2>/dev/null || { print "mac unreachable"; exit 3 }

mac_test=$lib/../mac_test/mac_test.py
claim_output=$("$lib/py" "$mac_test" claim --pid $$ --what "mac run ${sha[1,9]}" --worktree $repo --wait $claim_seconds)
claim_status=$?
if (( claim_status == 10 || claim_status == 11 || claim_status == 12 )); then
  print -r -- $claim_output
  exit 9
fi
(( claim_status == 0 )) || { print -r -- $claim_output; exit $claim_status }
mac_claimed=1
release_mac_claim() {
  (( mac_claimed )) || return 0
  local cleanup_failed=0 confirmation
  if [[ -n $clone_task ]]; then
    if [[ -n $clone ]] && confirmation=$(on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/hana_instances/hana_instances.py stop-checkout $clone_arg") && [[ ${confirmation##*$'\n'} == 'hana-instances: checkout clear' ]]; then
      if ! on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py end --task $clone_task" >/dev/null; then
        print -u2 "mac_run: scratch cleanup failed; kept task $clone_task"
        cleanup_failed=1
      elif ! on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py unregister-remote --host localhost --path $clone_arg" >/dev/null; then
        print -u2 "mac_run: remote clone registry cleanup failed"
        cleanup_failed=1
      elif ! "$lib/py" $copies_script unregister-remote --host $host --path $clone; then
        print -u2 "mac_run: local clone registry cleanup failed"
        cleanup_failed=1
      fi
    else
      print -u2 "mac_run: process exit not confirmed; kept checkout $clone and task $clone_task${confirmation:+: $confirmation}"
      cleanup_failed=1
    fi
  fi
  "$lib/py" "$mac_test" release --pid $$ >/dev/null 2>&1
  mac_claimed=0
  return $cleanup_failed
}
finish_mac_run() {
  local exit_status=$?
  trap - EXIT
  [[ -z $bundle ]] || rm -f $bundle
  if ! release_mac_claim && (( exit_status == 0 )); then
    exit_status=4
  fi
  exit $exit_status
}
stop_mac_run() {
  local exit_status=$1
  trap '' INT TERM HUP
  if [[ -n $clone ]]; then
    ssh -o ConnectTimeout=$connect_seconds -o BatchMode=yes $host "for pid in \$(pgrep -f '[c]argo build'); do cwd=\$(lsof -a -p \"\$pid\" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'); case \"\$cwd\" in $clone_arg|$clone_arg/*) pkill -TERM -P \"\$pid\" 2>/dev/null || true; kill \"\$pid\" 2>/dev/null || true;; esac; done; rm -f $bundle_arg $launch_arg $status_arg" </dev/null >/dev/null 2>&1 || true
  fi
  [[ -z $bundle ]] || rm -f $bundle
  exit $exit_status
}
trap finish_mac_run EXIT
trap 'stop_mac_run 130' INT
trap 'stop_mac_run 143' TERM
trap 'stop_mac_run 129' HUP
sha=$(git -C $repo rev-parse --verify "$sha^{commit}") || exit 2

# The Mac cannot fetch from GitHub over ssh, so the commits go as a bundle.
# When the source already holds the commit, no bundle is needed.
clone_task=gate:mac-run-${sha}-$$
copies_script=$lib/../copies/copies.py
copy_owner=$(python3 -c 'import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2])); from scripts.copies import copies; print(copies.source_owner(Path(sys.argv[2])))' $copies_script $repo) || exit 4
copy_owner_arg=${(q)copy_owner}
copy_session=${owner:-pid-$$}
copy_session_arg=${(q)copy_session}
clone=$(on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py clone $source_arg --task $clone_task --name hana --owner $copy_owner_arg --repository hana --session $copy_session_arg") || exit 4
[[ $clone == /* && $clone != *$'\n'* ]] || { print -u2 'mac_run: scratch clone returned no absolute checkout'; exit 4 }
clone_arg=${(q)clone}
registry_args=(--repository hana --owner $copy_owner --host $host --path $clone --session $copy_session)
mirror_lease=$("$lib/py" $copies_script register-remote "${registry_args[@]}" --shell-lease) || exit 4
exec 8>"$mirror_lease"
flock 8
"$lib/py" $copies_script register-remote "${registry_args[@]}" || exit 4
base=$(ssh $host "git -C $clone_arg rev-parse HEAD")
if git -C $repo merge-base --is-ancestor $sha $base 2>/dev/null; then
  ssh $host "git -C $clone_arg checkout -q --detach $sha"
else
  ref=refs/mac-run/tip
  bundle=$(mktemp --suffix=.bundle)
  git -C $repo update-ref $ref $sha
  if git -C $repo cat-file -e "$base^{commit}" 2>/dev/null && [[ $base != $sha ]]; then
    git -C $repo bundle create -q $bundle $ref "^$base"
  else
    git -C $repo bundle create -q $bundle $ref
  fi
  bundle_exit=$?
  git -C $repo update-ref -d $ref
  (( bundle_exit == 0 )) || { rm -f $bundle; exit 4 }
  scp -q $bundle "$host:$remote_bundle" || { rm -f $bundle; exit 4 }
  rm -f $bundle

  ssh $host "git -C $clone_arg fetch -q $bundle_arg $ref && git -C $clone_arg checkout -q --detach FETCH_HEAD; rm -f $bundle_arg"
fi
head=$(ssh $host "git -C $clone_arg rev-parse HEAD")
print "HEAD=$head"
[[ $head == $sha ]] || { print "wrong tree: wanted $sha"; exit 5 }

on_mac "cd $clone_arg && MACOSX_DEPLOYMENT_TARGET=$deployment_target ~/.cargo/bin/cargo build -q -p hana" || { print "build failed"; exit 6 }

# perl's alarm stops hana after the hold; 142 (SIGALRM) means it stayed up.
# Run outside cargo, so Bevy needs the asset root that `cargo run` would give it.
# Started over ssh, hana gets no camera access and its cameras stay dark, so it
# starts from Terminal, which holds the grant. `open` returns at once, so the
# .command writes hana's exit status to a file. User, 2026-10-07.
ssh $host "rm -f $status_arg; cat > $launch_arg; chmod +x $launch_arg" <<EOF
#!/bin/zsh
cd $clone_arg
HANA_TEST_PURPOSE=$purpose_arg HANA_TEST_OWNER=$owner_arg BEVY_ASSET_ROOT=$clone_arg/crates/hana BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold $clone_arg/target/debug/hana > $log_arg 2>&1
echo \$? > $status_arg
exit 0
EOF
ssh $host "open -a Terminal $launch_arg"
run_exit=$(ssh $host "for i in {1..$(( hold + status_grace ))}; do [ -f $status_arg ] && break; sleep $poll_seconds; done; cat $status_arg 2>/dev/null; rm -f $launch_arg $status_arg")
if [[ $run_exit != <-> ]]; then
  on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/hana_instances/hana_instances.py stop-checkout $clone_arg" || exit 7
  run_exit=255
fi
if (( run_exit == 142 )); then
  print "hana stayed up ${hold}s"
  result=0
else
  print "hana exited early: $run_exit"
  ssh $host "grep -v 'CommandQueue has un-applied' $log_arg | tail -$log_lines"
  result=7
fi

failed=0
for entry in $examples; do
  parts=(${=entry})
  args="-p $parts[1] --example $parts[2]"
  (( $#parts == 3 )) && args="$args --features $parts[3]"
  example_log=${example_log_prefix}$parts[2].log
  example_log_arg=${(q)example_log}
  if ! on_mac "cd $clone_arg && MACOSX_DEPLOYMENT_TARGET=$deployment_target ~/.cargo/bin/cargo build -q $args" > /dev/null 2>&1; then
    print "example $parts[2]: build failed"
    failed=1
    continue
  fi
  # Run the built binary, as for hana: the Mac's cargo is a wrapper script that
  # waits on its child, so an alarm on `cargo run` never reaches the example.
  # The pkill catches a survivor; the bracket keeps it from matching its shell.
  on_mac "cd $clone_arg && BEVY_ASSET_ROOT=$clone_arg/crates/$parts[1] BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $example_hold $clone_arg/target/debug/examples/$parts[2] > $example_log_arg 2>&1"
  example_exit=$?
  example_pattern=${clone}/'[t]arget/debug/examples/'$parts[2]
  ssh $host "pkill -f ${(q)example_pattern}"
  if (( example_exit == 142 )); then
    print "example $parts[2]: stayed up ${example_hold}s  (cargo run $args)"
  else
    print "example $parts[2]: exited early: $example_exit"
    ssh $host "tail -$example_log_lines $example_log_arg"
    failed=1
  fi
done

(( result == 0 && failed )) && result=8
exit $result
