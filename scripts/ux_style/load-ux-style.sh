#!/usr/bin/env zsh
# Load the Hana UX guide: rule text, a one-line-per-rule checklist, or both.
set -euo pipefail

usage() {
  print -u2 "usage: load-ux-style.sh [--guide <dir>] [--checklist] [--tags <t1,t2>] [--rule <stem>]..."
  exit 2
}

guide="$HOME/rust/hanadocs/ux"
checklist=false
typeset -a wanted_tags=() wanted_rules=()

while (( $# )); do
  case "$1" in
    --guide) (( $# >= 2 )) || usage; guide="$2"; shift 2 ;;
    --checklist) checklist=true; shift ;;
    --tags) (( $# >= 2 )) || usage; wanted_tags=(${(s:,:)${2// /}}); shift 2 ;;
    --rule) (( $# >= 2 )) || usage; wanted_rules+=("$2"); shift 2 ;;
    *) usage ;;
  esac
done

[[ -d "$guide" ]] || { print -u2 "error: no guide folder at $guide"; usage; }

# Print "title<US>test<US>tag,tag" from one file. Tags may be inline
# (`tags: [a, b]`) or an Obsidian list; `test` may be quoted either way.
read_meta() {
  awk -v q="'" '
    function unquote(v) {
      sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
      if (v ~ /^".*"$/) { v = substr(v, 2, length(v) - 2); gsub(/\\"/, "\"", v) }
      else if (v ~ "^" q ".*" q "$") { v = substr(v, 2, length(v) - 2); gsub(q q, q, v) }
      return v
    }
    NR == 1 && /^---$/ { fm = 1; next }
    fm && /^---$/ { fm = 0; body = 1; next }
    fm && /^[a-z_]+:/ { in_tags = 0 }
    fm && /^test:/ { v = $0; sub(/^test:/, "", v); test = unquote(v) }
    fm && /^tags:/ {
      v = $0; sub(/^tags:[[:space:]]*/, "", v)
      if (v == "") { in_tags = 1 } else { gsub(/\[|\]|[[:space:]]/, "", v); tags = v }
      next
    }
    fm && in_tags && /^[[:space:]]*-/ {
      v = $0; sub(/^[[:space:]]*-/, "", v); tags = tags (tags == "" ? "" : ",") unquote(v)
    }
    body && title == "" && /^## / { title = substr($0, 4) }
    END { printf "%s\037%s\037%s\n", title, test, tags }
  ' "$1"
}

strip_frontmatter() {
  awk 'NR == 1 && /^---$/ { fm = 1; next } fm && /^---$/ { fm = 0; next } !fm' "$1"
}

# A rule is a .md file whose frontmatter carries `test:`; README and notes are skipped.
typeset -A rule_file=() rule_line=()
typeset -a stems=()
for file in "$guide"/*.md(N); do
  IFS=$'\037' read -r title rule_test tags <<< "$(read_meta "$file")"
  [[ -n "$rule_test" ]] || continue
  stem="${file:t:r}"
  rule_file[$stem]="$file"
  if (( ${#wanted_tags} )); then
    rule_tags=(${(s:,:)tags})
    matched=false
    for tag in "${wanted_tags[@]}"; do
      (( ${rule_tags[(Ie)$tag]} )) && matched=true
    done
    [[ "$matched" == true ]] || continue
  fi
  stems+=("$stem")
  rule_line[$stem]="$stem — $title — $rule_test"
done

if (( ${#wanted_tags} && ! ${#stems} )); then
  print -u2 "error: no rule carries any of: ${(j:, :)wanted_tags}"
  usage
fi

for stem in "${wanted_rules[@]}"; do
  [[ -n "${rule_file[$stem]:-}" ]] || { print -u2 "error: no rule named $stem"; usage; }
done

print_checklist() {
  for stem in "${stems[@]}"; do
    print -r -- "${rule_line[$stem]}"
  done
}

if (( ${#wanted_rules} )) || [[ "$checklist" == true ]]; then
  for stem in "${wanted_rules[@]}"; do
    strip_frontmatter "${rule_file[$stem]}"
    print
  done
  [[ "$checklist" == true ]] && print_checklist
  exit 0
fi

for stem in "${stems[@]}"; do
  strip_frontmatter "${rule_file[$stem]}"
  print
done
print '=== UX_CHECKLIST ==='
print_checklist
