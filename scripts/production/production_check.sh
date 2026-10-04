#!/usr/bin/env zsh
# Exit 0 while a production runs, 2 when it has wrapped, 1 otherwise.

setopt no_unset pipe_fail extended_glob
(( $# == 1 )) || exit 1
[[ -r $1 ]] || exit 1

for line in "${(@f)$(<$1)}"; do
  if [[ $line == *'Status: PRODUCTION — '* ]]; then
    doc_status=${${line#*'Status: PRODUCTION — '}%%[^a-z]*}
    case $doc_status in
      running) exit 0 ;;
      wrapped) exit 2 ;;
      *) exit 1 ;;
    esac
  fi
done
exit 1
