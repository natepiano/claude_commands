#!/usr/bin/env bash
set -euo pipefail

list_files=false
project_root=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list-files)
      list_files=true
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
      echo "usage: load-rust-style.sh [--list-files] [--project-root PATH]" >&2
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

declare -a style_files=()
declare -a global_style_files=()
declare -a repo_style_files=()

if [[ -d "$global_style_dir" ]]; then
  while IFS= read -r file; do
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

if [[ "$list_files" == true ]]; then
  if [[ ${#style_files[@]} -gt 0 ]]; then
    printf '%s\n' "${style_files[@]}"
  fi
  exit 0
fi

strip_frontmatter() {
  awk 'NR==1 && /^---$/ { skip=1; next } skip && /^---$/ { skip=0; next } !skip' "$1"
}

total_lines=0
global_lines=0
repo_lines=0

pluralize_file() {
  local count="$1"
  if [[ "$count" -eq 1 ]]; then
    printf 'file'
  else
    printf 'files'
  fi
}

if [[ ${#style_files[@]} -gt 0 ]]; then
  for file in "${style_files[@]}"; do
    stripped="$(strip_frontmatter "$file")"
    printf '%s\n' "$stripped"
    total_lines=$((total_lines + $(printf '%s\n' "$stripped" | wc -l)))
  done
fi

if [[ ${#global_style_files[@]} -gt 0 ]]; then
  for file in "${global_style_files[@]}"; do
    global_lines=$((global_lines + $(strip_frontmatter "$file" | wc -l)))
  done
fi

if [[ ${#repo_style_files[@]} -gt 0 ]]; then
  for file in "${repo_style_files[@]}"; do
    repo_lines=$((repo_lines + $(strip_frontmatter "$file" | wc -l)))
  done
fi

total_files="${#style_files[@]}"
global_files="${#global_style_files[@]}"
repo_files="${#repo_style_files[@]}"

printf 'Rust style guide loaded — %d %s, %d lines (shared: %d %s, %d lines; project: %d %s, %d lines).\n' \
  "$total_files" \
  "$(pluralize_file "$total_files")" \
  "$total_lines" \
  "$global_files" \
  "$(pluralize_file "$global_files")" \
  "$global_lines" \
  "$repo_files" \
  "$(pluralize_file "$repo_files")" \
  "$repo_lines"
