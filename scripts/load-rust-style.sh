#!/bin/zsh
set -euo pipefail

list_files=false
project_root=""
shuffle=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list-files)
      list_files=true
      shift
      ;;
    --shuffle)
      shuffle=true
      shift
      ;;
    --project-root)
      if [[ $# -lt 2 ]]; then
        echo "error: --project-root requires a path" >&2
        exit 2
      fi
      project_root="$2"
      shift 2
      ;;
    *)
      echo "usage: load-rust-style.sh [--list-files] [--shuffle] [--project-root PATH]" >&2
      exit 2
      ;;
  esac
done

global_style_dir="$HOME/rust/nate_style/rust"
repo_root=""
repo_style_dir=""

if [[ -n "$project_root" ]]; then
  project_root="$(cd "$project_root" && pwd -P)"
  repo_root="$(git -C "$project_root" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -z "$repo_root" ]]; then
    repo_root="$project_root"
  fi
else
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi

if [[ -n "$repo_root" ]]; then
  repo_style_dir="$repo_root/docs/style"
fi

# Workspace member style dirs: $repo_root/docs/<member>/style/
declare -a member_style_dirs=()
if [[ -n "$repo_root" && -d "$repo_root/docs" ]]; then
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    member_style_dirs+=("$d")
  done < <(find "$repo_root/docs" -mindepth 2 -maxdepth 2 -type d -name style -print 2>/dev/null | LC_ALL=C sort)
fi

# ── Bevy detection ──────────────────────────────────────────────
# Check if any Cargo.toml in the repo depends on bevy.
is_bevy=false
if [[ -n "$repo_root" ]]; then
  if grep -rq '^\s*bevy\s*=' "$repo_root/Cargo.toml" 2>/dev/null; then
    is_bevy=true
  fi
fi

# Check frontmatter tags for the bevy tag. Returns 0 if tagged bevy.
has_tag() {
  local file="$1"
  local tag="$2"

  awk '
    NR==1 && /^---$/ { in_fm=1; next }
    in_fm && /^---$/ { exit }
    in_fm && /^[[:space:]]*-[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]*-[[:space:]]*/, "", value)
      if (value == tag) {
        found=1
        exit
      }
    }
    END { exit (found ? 0 : 1) }
  ' tag="$tag" "$file" 2>/dev/null
}

# Check frontmatter tags for the bevy tag. Returns 0 if tagged bevy.
has_bevy_tag() {
  has_tag "$1" bevy
}

# Read a top-level scalar frontmatter value (e.g. `mechanism: clippy`).
# Prints the value to stdout, or empty string if absent.
frontmatter_value() {
  local file="$1"
  local key="$2"

  awk '
    NR==1 && /^---$/ { in_fm=1; next }
    in_fm && /^---$/ { exit }
    in_fm {
      # Match `key: value` at top level (no leading whitespace).
      if ($0 ~ "^"key":[[:space:]]") {
        value=$0
        sub("^"key":[[:space:]]+", "", value)
        sub(/[[:space:]]+$/, "", value)
        # Strip surrounding quotes if any.
        if (value ~ /^".*"$/ || value ~ /^'\''.*'\''$/) {
          value = substr(value, 2, length(value) - 2)
        }
        print value
        exit
      }
    }
  ' key="$key" "$file" 2>/dev/null
}

# Returns 0 if the file is enforced by tooling that auto-fixes (no LLM needed).
# Skipped by the loader so /rust_style does not waste context on rules
# clippy/mend/rustfmt apply mechanically.
is_auto_fix() {
  local file="$1"
  local mech mode
  mech="$(frontmatter_value "$file" mechanism)"
  mode="$(frontmatter_value "$file" mode)"
  case "$mech" in
    clippy|mend|rustfmt)
      [[ "$mode" == "auto" ]] && return 0
      ;;
  esac
  return 1
}

# Emit one wikilink stem per line from a file's `see_also:` frontmatter entry.
# Supports single-line `see_also: "[[stem]]"` and multi-line list forms:
#   see_also:
#     - "[[stem-one]]"
#     - "[[stem-two]]"
extract_see_also() {
  awk '
    function unwrap(v) {
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      if (v ~ /^".*"$/ || v ~ /^'\''.*'\''$/) {
        v = substr(v, 2, length(v) - 2)
      }
      if (v ~ /^\[\[.*\]\]$/) {
        v = substr(v, 3, length(v) - 4)
      }
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      return v
    }
    NR==1 && /^---$/ { in_fm=1; next }
    in_fm && /^---$/ { exit }
    in_fm {
      # New top-level key closes any list-collection state
      if ($0 ~ /^[a-z_]+:/) {
        if ($0 ~ /^see_also:/) {
          value = $0
          sub(/^see_also:[[:space:]]*/, "", value)
          if (value != "") {
            v = unwrap(value)
            if (v != "") print v
            in_see_also = 0
          } else {
            in_see_also = 1
          }
        } else {
          in_see_also = 0
        }
        next
      }
      if (in_see_also && $0 ~ /^[[:space:]]*-[[:space:]]*/) {
        value = $0
        sub(/^[[:space:]]*-[[:space:]]*/, "", value)
        v = unwrap(value)
        if (v != "") print v
      }
    }
  ' "$1"
}

# ── Collect style files ─────────────────────────────────────────
declare -a style_files=()
declare -a global_style_files=()
declare -a repo_style_files=()
declare -a member_style_files=()
declare -a non_negotiable_files=()
skipped_bevy=0
skipped_auto_fix=0

if [[ -d "$global_style_dir" ]]; then
  while IFS= read -r file; do
    if [[ "$is_bevy" == false ]] && has_bevy_tag "$file"; then
      skipped_bevy=$((skipped_bevy + 1))
      continue
    fi
    if is_auto_fix "$file"; then
      skipped_auto_fix=$((skipped_auto_fix + 1))
      continue
    fi
    global_style_files+=("$file")
    if has_tag "$file" non-negotiable; then
      non_negotiable_files+=("$file")
    fi
    style_files+=("$file")
  done < <(find "$global_style_dir" -maxdepth 1 -type f -name '*.md' -print | LC_ALL=C sort)
fi

if [[ -n "$repo_style_dir" && -d "$repo_style_dir" ]]; then
  while IFS= read -r file; do
    if is_auto_fix "$file"; then
      skipped_auto_fix=$((skipped_auto_fix + 1))
      continue
    fi
    if has_tag "$file" non-negotiable; then
      non_negotiable_files+=("$file")
    fi
    repo_style_files+=("$file")
    style_files+=("$file")
  done < <(find "$repo_style_dir" -maxdepth 1 -type f -name '*.md' -print | LC_ALL=C sort)
fi

for dir in "${member_style_dirs[@]}"; do
  while IFS= read -r file; do
    if is_auto_fix "$file"; then
      skipped_auto_fix=$((skipped_auto_fix + 1))
      continue
    fi
    if has_tag "$file" non-negotiable; then
      non_negotiable_files+=("$file")
    fi
    member_style_files+=("$file")
    style_files+=("$file")
  done < <(find "$dir" -maxdepth 1 -type f -name '*.md' -print | LC_ALL=C sort)
done

typeset -A file_non_negotiable=()
typeset -A file_see_also=()  # file -> newline-separated wikilink stems
typeset -A stem_to_file=()   # wikilink stem -> resolved file path
for file in "${style_files[@]}"; do
  if has_tag "$file" non-negotiable; then
    file_non_negotiable[$file]=1
  fi
  stem="${file:t:r}"  # basename without .md
  stem_to_file[$stem]="$file"
  refs="$(extract_see_also "$file")"
  if [[ -n "$refs" ]]; then
    file_see_also[$file]="$refs"
  fi
done

# ── Shuffle if requested ────────────────────────────────────────
# Non-negotiable files pin to the top in stable order; everything else shuffles.
if [[ "$shuffle" == true && ${#style_files[@]} -gt 1 ]]; then
  pinned=()
  shuffle_pool=()
  for file in "${style_files[@]}"; do
    if [[ -n "${file_non_negotiable[$file]:-}" ]]; then
      pinned+=("$file")
    else
      shuffle_pool+=("$file")
    fi
  done

  shuffled_pool=()
  while IFS= read -r entry; do
    shuffled_pool+=("$entry")
  done < <(printf '%s\n' "${shuffle_pool[@]}" | sort -R)

  style_files=("${pinned[@]}" "${shuffled_pool[@]}")
fi

if [[ "$list_files" == true ]]; then
  if [[ ${#style_files[@]} -gt 0 ]]; then
    printf '%s\n' "${style_files[@]}"
  fi
  exit 0
fi

# ── Helpers ─────────────────────────────────────────────────────
strip_frontmatter() {
  awk 'NR==1 && /^---$/ { skip=1; next } skip && /^---$/ { skip=0; next } !skip' "$1"
}

extract_title() {
  grep '^#' "$1" 2>/dev/null | head -1 | sed -E 's/^#+ //' || true
}

pluralize_file() {
  local count="$1"
  if [[ "$count" -eq 1 ]]; then
    printf 'file'
  else
    printf 'files'
  fi
}

# ── Count lines first (summary must appear before content) ──────
total_lines=0
global_lines=0
repo_lines=0
member_lines=0

if [[ ${#global_style_files[@]} -gt 0 ]]; then
  for file in "${global_style_files[@]}"; do
    lines=$(strip_frontmatter "$file" | wc -l)
    global_lines=$((global_lines + lines))
    total_lines=$((total_lines + lines))
  done
fi

if [[ ${#repo_style_files[@]} -gt 0 ]]; then
  for file in "${repo_style_files[@]}"; do
    lines=$(strip_frontmatter "$file" | wc -l)
    repo_lines=$((repo_lines + lines))
    total_lines=$((total_lines + lines))
  done
fi

if [[ ${#member_style_files[@]} -gt 0 ]]; then
  for file in "${member_style_files[@]}"; do
    lines=$(strip_frontmatter "$file" | wc -l)
    member_lines=$((member_lines + lines))
    total_lines=$((total_lines + lines))
  done
fi

# ── Summary line (first, so it survives output truncation) ──────
total_files="${#style_files[@]}"
global_files="${#global_style_files[@]}"
repo_files="${#repo_style_files[@]}"
member_files="${#member_style_files[@]}"
non_negotiable_count="${#non_negotiable_files[@]}"

bevy_note=""
if [[ "$is_bevy" == true ]]; then
  bevy_note=" (bevy project)"
elif [[ "$skipped_bevy" -gt 0 ]]; then
  bevy_note=" (skipped $skipped_bevy bevy rules)"
fi
auto_fix_note=""
if [[ "$skipped_auto_fix" -gt 0 ]]; then
  auto_fix_note=" (skipped $skipped_auto_fix tool-enforced rules)"
fi

printf 'Rust style guide loaded — %d %s, %d lines (shared: %d %s, %d lines; project: %d %s, %d lines; members: %d %s, %d lines; non-negotiable: %d)%s%s.\n\n' \
  "$total_files" \
  "$(pluralize_file "$total_files")" \
  "$total_lines" \
  "$global_files" \
  "$(pluralize_file "$global_files")" \
  "$global_lines" \
  "$repo_files" \
  "$(pluralize_file "$repo_files")" \
  "$repo_lines" \
  "$member_files" \
  "$(pluralize_file "$member_files")" \
  "$member_lines" \
  "$non_negotiable_count" \
  "$bevy_note" \
  "$auto_fix_note"

if [[ ${#member_style_dirs[@]} -gt 0 ]]; then
  printf 'Workspace member style dirs loaded (some rules may not apply to the file you are editing — judge from context):\n'
  for dir in "${member_style_dirs[@]}"; do
    printf '  - %s\n' "${dir#$repo_root/}"
  done
  printf '\n'
fi

# ── Output rule content ─────────────────────────────────────────
if [[ ${#style_files[@]} -gt 0 ]]; then
  for file in "${style_files[@]}"; do
    strip_frontmatter "$file"
    if [[ -n "${file_see_also[$file]:-}" ]]; then
      while IFS= read -r stem; do
        [[ -z "$stem" ]] && continue
        ref_file="${stem_to_file[$stem]:-}"
        if [[ -n "$ref_file" && -f "$ref_file" ]]; then
          printf '\n### Related style guidance (via see_also → %s)\n\n' "$stem"
          strip_frontmatter "$ref_file"
        fi
      done <<< "${file_see_also[$file]}"
    fi
  done
fi

# ── Emit checklist ──────────────────────────────────────────────
printf '\n=== STYLE_CHECKLIST ===\n'
rule_num=0
for file in "${style_files[@]}"; do
  title="$(extract_title "$file")"
  if [[ -n "$title" ]]; then
    rule_num=$((rule_num + 1))
    if [[ -n "${file_non_negotiable[$file]:-}" ]]; then
      printf '%d. %s [non-negotiable]\n' "$rule_num" "$title"
    else
      printf '%d. %s\n' "$rule_num" "$title"
    fi
  fi
done
