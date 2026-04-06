#!/usr/bin/env bash
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

# ── Bevy detection ──────────────────────────────────────────────
# Check if any Cargo.toml in the repo depends on bevy.
is_bevy=false
if [[ -n "$repo_root" ]]; then
  if grep -rq '^\s*bevy\s*=' "$repo_root/Cargo.toml" 2>/dev/null; then
    is_bevy=true
  fi
fi

# Check frontmatter tags for the bevy tag. Returns 0 if tagged bevy.
has_bevy_tag() {
  # Read lines between the first --- and the second ---.
  # Look for "- bevy" in that range.
  awk '
    NR==1 && /^---$/ { in_fm=1; next }
    in_fm && /^---$/ { exit 1 }
    in_fm && /^- bevy$/ { found=1; exit 0 }
    END { exit (found ? 0 : 1) }
  ' "$1" 2>/dev/null
}

# ── Collect style files ─────────────────────────────────────────
declare -a style_files=()
declare -a global_style_files=()
declare -a repo_style_files=()
skipped_bevy=0

if [[ -d "$global_style_dir" ]]; then
  while IFS= read -r file; do
    if [[ "$is_bevy" == false ]] && has_bevy_tag "$file"; then
      skipped_bevy=$((skipped_bevy + 1))
      continue
    fi
    global_style_files+=("$file")
    style_files+=("$file")
  done < <(find "$global_style_dir" -maxdepth 1 -type f -name '*.md' -print | LC_ALL=C sort)
fi

if [[ -n "$repo_style_dir" && -d "$repo_style_dir" ]]; then
  while IFS= read -r file; do
    repo_style_files+=("$file")
    style_files+=("$file")
  done < <(find "$repo_style_dir" -maxdepth 1 -type f -name '*.md' -print | LC_ALL=C sort)
fi

# ── Shuffle if requested ────────────────────────────────────────
if [[ "$shuffle" == true && ${#style_files[@]} -gt 1 ]]; then
  shuffled=()
  while IFS= read -r file; do
    shuffled+=("$file")
  done < <(printf '%s\n' "${style_files[@]}" | sort -R)
  style_files=("${shuffled[@]}")
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

# ── Summary line (first, so it survives output truncation) ──────
total_files="${#style_files[@]}"
global_files="${#global_style_files[@]}"
repo_files="${#repo_style_files[@]}"

bevy_note=""
if [[ "$is_bevy" == true ]]; then
  bevy_note=" (bevy project)"
elif [[ "$skipped_bevy" -gt 0 ]]; then
  bevy_note=" (skipped $skipped_bevy bevy rules)"
fi

printf 'Rust style guide loaded — %d %s, %d lines (shared: %d %s, %d lines; project: %d %s, %d lines)%s.\n\n' \
  "$total_files" \
  "$(pluralize_file "$total_files")" \
  "$total_lines" \
  "$global_files" \
  "$(pluralize_file "$global_files")" \
  "$global_lines" \
  "$repo_files" \
  "$(pluralize_file "$repo_files")" \
  "$repo_lines" \
  "$bevy_note"

# ── Output rule content ─────────────────────────────────────────
if [[ ${#style_files[@]} -gt 0 ]]; then
  for file in "${style_files[@]}"; do
    strip_frontmatter "$file"
  done
fi

# ── Emit checklist ──────────────────────────────────────────────
printf '\n=== STYLE_CHECKLIST ===\n'
rule_num=0
for file in "${style_files[@]}"; do
  title="$(extract_title "$file")"
  if [[ -n "$title" ]]; then
    rule_num=$((rule_num + 1))
    printf '%d. %s\n' "$rule_num" "$title"
  fi
done
