#!/usr/bin/env zsh
# Run a sha on the Mac: ship it from this machine, build hana, run it, check it
# stays up for SECONDS, then stop it. User, 2026-10-04: after each green CI on
# the merge branch, when the Mac is reachable.
#
# Then it builds each demo example with the same cargo arguments you would
# type, so `cargo run` in the clone starts at once, and runs it for
# EXAMPLE_HOLD seconds. User, 2026-10-05: the Ian Hubert demo runs from this clone.
#
# Usage: mac_run.sh <repo dir> <sha> [seconds, default 60]
# Exit: 0 all stayed up; 3 Mac unreachable; 5 wrong tree; 6 build failed;
# 7 hana exited early; 8 hana stayed up but an example failed; 9 Mac blocked,
# busy with a test, or coordination state unreadable.
set -o pipefail
repo=$1 sha=$2 hold=${3:-60}
host=natemccoy@mac
# A standing clone, so builds are incremental. Never ~/rust/hana, the user's.
clone=rust/hana_catalyst_mac
port=15710
log=/tmp/mac_run_hana.log
launch=/tmp/mac_run_hana.command
status_file=/tmp/mac_run_hana.status
bundle=""
example_hold=20
# crate, example, then its required feature when it has one.
examples=(
  "hana_diegetic typography"
  "hana_diegetic units"
  "hana_diegetic widgets"
  "hana_diegetic diegetic_text_stress"
  "hana_conduit playground"
  "hana_lagrange showcase fit_overlay"
  "hana_liminal all_modes"
  "hana_valence staggered_unfold"
  "hana_mimesis_tools show_beam"
)

# Tailscale SSH serves the Mac, and its exit status is always 0, so a command
# whose status matters prints it as its last line and on_mac returns that.
on_mac() {
  local out code
  out=$(ssh $host "$1; echo mac_exit=\$?")
  print -rn -- "${out%mac_exit=*}"
  code=${out##*mac_exit=}
  [[ $code == <-> ]] || code=255
  return $code
}

ssh -o ConnectTimeout=6 -o BatchMode=yes $host true 2>/dev/null || { print "mac unreachable"; exit 3 }

mac_test=$HOME/.claude/scripts/mac_test/mac_test.py
claim_output=$(python3 $mac_test claim --pid $$ --what "mac run ${sha[1,9]}" --wait 600)
claim_status=$?
if (( claim_status == 10 || claim_status == 11 || claim_status == 12 )); then
  print -r -- $claim_output
  exit 9
fi
(( claim_status == 0 )) || { print -r -- $claim_output; exit $claim_status }
mac_claimed=1
release_mac_claim() {
  (( mac_claimed )) || return 0
  python3 $mac_test release --pid $$ >/dev/null 2>&1
  mac_claimed=0
  return 0
}
stop_mac_run() {
  local exit_status=$1
  trap '' INT TERM HUP
  ssh -o ConnectTimeout=6 -o BatchMode=yes $host "for pid in \$(pgrep -f '[c]argo build'); do cwd=\$(lsof -a -p \"\$pid\" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'); case \"\$cwd\" in \"\$HOME/$clone\"|\"\$HOME/$clone\"/*) pkill -TERM -P \"\$pid\" 2>/dev/null || true; kill \"\$pid\" 2>/dev/null || true;; esac; done; pkill -f '[h]ana_catalyst_mac/target/debug/' 2>/dev/null || true; rm -f /tmp/mac_run.bundle $launch $status_file" </dev/null >/dev/null 2>&1 || true
  [[ -z $bundle ]] || rm -f $bundle
  release_mac_claim
  exit $exit_status
}
trap release_mac_claim EXIT
trap 'stop_mac_run 130' INT
trap 'stop_mac_run 143' TERM
trap 'stop_mac_run 129' HUP
sha=$(git -C $repo rev-parse --verify "$sha^{commit}") || exit 2

# The Mac cannot fetch from GitHub over ssh, so the commits go as a bundle.
on_mac "test -d $clone/.git || git clone -q ~/rust/hana $clone" || exit 4
base=$(ssh $host "git -C $clone rev-parse HEAD")
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
scp -q $bundle $host:/tmp/mac_run.bundle || { rm -f $bundle; exit 4 }
rm -f $bundle

ssh $host "git -C $clone fetch -q /tmp/mac_run.bundle $ref && git -C $clone checkout -q --detach FETCH_HEAD; rm -f /tmp/mac_run.bundle"
head=$(ssh $host "git -C $clone rev-parse HEAD")
print "HEAD=$head"
[[ $head == $sha ]] || { print "wrong tree: wanted $sha"; exit 5 }

on_mac "cd $clone && ~/.cargo/bin/cargo build -q -p hana" || { print "build failed"; exit 6 }

# perl's alarm stops hana after the hold; 142 (SIGALRM) means it stayed up.
# Run outside cargo, so Bevy needs the asset root that `cargo run` would give it.
# Started over ssh, hana gets no camera access and its cameras stay dark, so it
# starts from Terminal, which holds the grant. `open` returns at once, so the
# .command writes hana's exit status to a file. User, 2026-10-07.
ssh $host "rm -f $status_file; cat > $launch; chmod +x $launch" <<EOF
#!/bin/zsh
cd ~/$clone
BEVY_ASSET_ROOT=\$HOME/$clone/crates/hana BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold \$HOME/$clone/target/debug/hana > $log 2>&1
echo \$? > $status_file
exit 0
EOF
ssh $host "open -a Terminal $launch"
run_exit=$(ssh $host "for i in {1..$(( hold + 60 ))}; do [ -f $status_file ] && break; sleep 1; done; cat $status_file 2>/dev/null; rm -f $launch $status_file")
if [[ $run_exit != <-> ]]; then
  ssh $host "pkill -f '[h]ana_catalyst_mac/target/debug/hana'"
  run_exit=255
fi
if (( run_exit == 142 )); then
  print "hana stayed up ${hold}s"
  result=0
else
  print "hana exited early: $run_exit"
  ssh $host "grep -v 'CommandQueue has un-applied' $log | tail -20"
  result=7
fi

failed=0
for entry in $examples; do
  parts=(${=entry})
  args="-p $parts[1] --example $parts[2]"
  (( $#parts == 3 )) && args="$args --features $parts[3]"
  example_log=/tmp/mac_run_$parts[2].log
  if ! on_mac "cd $clone && ~/.cargo/bin/cargo build -q $args" > /dev/null 2>&1; then
    print "example $parts[2]: build failed"
    failed=1
    continue
  fi
  # Run the built binary, as for hana: the Mac's cargo is a wrapper script that
  # waits on its child, so an alarm on `cargo run` never reaches the example.
  # The pkill catches a survivor; the bracket keeps it from matching its shell.
  on_mac "cd $clone && BEVY_ASSET_ROOT=\$HOME/$clone/crates/$parts[1] BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $example_hold \$HOME/$clone/target/debug/examples/$parts[2] > $example_log 2>&1"
  example_exit=$?
  ssh $host "pkill -f '[h]ana_catalyst_mac/target/debug/examples/$parts[2]'"
  if (( example_exit == 142 )); then
    print "example $parts[2]: stayed up ${example_hold}s  (cargo run $args)"
  else
    print "example $parts[2]: exited early: $example_exit"
    ssh $host "tail -10 $example_log"
    failed=1
  fi
done

(( result == 0 && failed )) && result=8
exit $result
