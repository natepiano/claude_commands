#!/usr/bin/env bash
set -euo pipefail

SYSROOT_VERSION="debian-bookworm-amd64-v4"
DEBIAN_MIRROR="${VALIDATE_LINUX_DEBIAN_MIRROR:-https://deb.debian.org/debian}"
DEBIAN_SUITE="${VALIDATE_LINUX_DEBIAN_SUITE:-bookworm}"
DEBIAN_ARCH="${VALIDATE_LINUX_DEBIAN_ARCH:-amd64}"
TARGET_TRIPLE="x86_64-linux-gnu"

default_download_cache_root() {
  local os
  os="$(uname -s 2>/dev/null || true)"
  if [ "$os" = "Darwin" ] && [ -n "${HOME:-}" ]; then
    printf '%s\n' "${HOME}/Library/Caches/validate-and-push/linux-sysroot"
  elif [ -n "${XDG_CACHE_HOME:-}" ]; then
    printf '%s\n' "${XDG_CACHE_HOME}/validate-and-push/linux-sysroot"
  elif [ -n "${HOME:-}" ]; then
    printf '%s\n' "${HOME}/.cache/validate-and-push/linux-sysroot"
  else
    printf '%s\n' "/tmp/validate-and-push/linux-sysroot"
  fi
}

if [ -n "${VALIDATE_LINUX_SYSROOT:-}" ]; then
  SYSROOT="${VALIDATE_LINUX_SYSROOT}"
else
  if [ -n "${VALIDATE_TARGET_DIR:-}" ]; then
    TARGET_DIR="${VALIDATE_TARGET_DIR}"
  elif [ -n "${CARGO_TARGET_DIR:-}" ]; then
    TARGET_DIR="${CARGO_TARGET_DIR}"
  else
    TARGET_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/target"
  fi
  SYSROOT="${TARGET_DIR}/validate-linux-sysroot"
fi

DOWNLOAD_CACHE_ROOT="${VALIDATE_LINUX_DOWNLOAD_CACHE:-$(default_download_cache_root)}"
CACHE_KEY="${DEBIAN_SUITE}-${DEBIAN_ARCH}-${SYSROOT_VERSION}"
CACHE_DIR="${DOWNLOAD_CACHE_ROOT}/${CACHE_KEY}"
LEGACY_CACHE_DIR="${SYSROOT}/.cache"
INDEX_DIR="${CACHE_DIR}/debian-${DEBIAN_SUITE}-${DEBIAN_ARCH}"
PACKAGES_FILE="${INDEX_DIR}/Packages"
DOWNLOAD_STAMP_FILE="${CACHE_DIR}/.download-stamp"
STAMP_FILE="${SYSROOT}/.stamp"
ENV_FILE="${SYSROOT}/env.sh"
SYSROOT_LOCK_DIR="${SYSROOT}/.lock"
CACHE_LOCK_DIR="${CACHE_DIR}/.lock"
REQUESTED_PACKAGES="libwayland-dev libasound2-dev libasound2 libffi-dev libudev-dev libudev1"
REQUIRED_PC_FILES="
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/pkgconfig/wayland-client.pc
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/pkgconfig/alsa.pc
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/libasound.so.2
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/pkgconfig/libffi.pc
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/pkgconfig/libudev.pc
${SYSROOT}/usr/lib/${TARGET_TRIPLE}/libudev.so.1
"

log() {
  printf '%s\n' "$*" >&2
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

quote_shell() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_env_file() {
  mkdir -p "$SYSROOT"
  {
    printf 'export VALIDATE_LINUX_SYSROOT='
    quote_shell "$SYSROOT"
    printf '\n'
    printf 'export PKG_CONFIG_SYSROOT_DIR='
    quote_shell "$SYSROOT"
    printf '\n'
    printf 'export PKG_CONFIG_LIBDIR='
    quote_shell "${SYSROOT}/usr/lib/${TARGET_TRIPLE}/pkgconfig:${SYSROOT}/usr/share/pkgconfig"
    printf '\n'
  } > "$ENV_FILE"
}

stamp_contents() {
  printf 'version=%s\n' "$SYSROOT_VERSION"
  printf 'mirror=%s\n' "$DEBIAN_MIRROR"
  printf 'suite=%s\n' "$DEBIAN_SUITE"
  printf 'arch=%s\n' "$DEBIAN_ARCH"
  printf 'packages=%s\n' "$REQUESTED_PACKAGES"
}

sysroot_is_ready() {
  [ -f "$STAMP_FILE" ] || return 1
  [ -f "$ENV_FILE" ] || return 1
  if ! stamp_contents | cmp -s - "$STAMP_FILE"; then
    return 1
  fi

  local pc_file
  for pc_file in $REQUIRED_PC_FILES; do
    [ -f "$pc_file" ] || return 1
  done
}

download_cache_is_ready() {
  [ -f "$PACKAGES_FILE" ] || return 1
  [ -f "$DOWNLOAD_STAMP_FILE" ] || return 1
  stamp_contents | cmp -s - "$DOWNLOAD_STAMP_FILE"
}

copy_legacy_cache_if_present() {
  if [ ! -d "$LEGACY_CACHE_DIR" ]; then
    return 0
  fi

  local legacy_index_dir="${LEGACY_CACHE_DIR}/debian-${DEBIAN_SUITE}-${DEBIAN_ARCH}"
  if [ -f "${legacy_index_dir}/Packages" ] && [ ! -f "$PACKAGES_FILE" ]; then
    mkdir -p "$INDEX_DIR"
    cp -p "${legacy_index_dir}/Packages" "$PACKAGES_FILE"
  fi
  if [ -f "${legacy_index_dir}/Packages.gz" ] && [ ! -f "${INDEX_DIR}/Packages.gz" ]; then
    mkdir -p "$INDEX_DIR"
    cp -p "${legacy_index_dir}/Packages.gz" "${INDEX_DIR}/Packages.gz"
  fi

  if [ -d "${LEGACY_CACHE_DIR}/debs" ]; then
    mkdir -p "${CACHE_DIR}/debs"
    local deb
    for deb in "${LEGACY_CACHE_DIR}/debs/"*.deb; do
      [ -f "$deb" ] || continue
      if [ ! -f "${CACHE_DIR}/debs/$(basename "$deb")" ]; then
        cp -p "$deb" "${CACHE_DIR}/debs/"
      fi
    done
  fi
}

download_package_index() {
  mkdir -p "$INDEX_DIR"
  if [ -f "$PACKAGES_FILE" ]; then
    return 0
  fi

  local packages_gz="${INDEX_DIR}/Packages.gz"
  local url="${DEBIAN_MIRROR}/dists/${DEBIAN_SUITE}/main/binary-${DEBIAN_ARCH}/Packages.gz"
  log "Downloading Debian package index: ${url}"
  curl -fsSL "$url" -o "$packages_gz"
  gzip -dc "$packages_gz" > "$PACKAGES_FILE"
}

package_field() {
  local package="$1"
  local field="$2"
  awk -v package="$package" -v field="$field" '
    BEGIN { RS = ""; FS = "\n" }
    {
      found = 0
      for (i = 1; i <= NF; i++) {
        if ($i == "Package: " package) {
          found = 1
        }
      }
      if (!found) {
        next
      }
      prefix = field ": "
      for (i = 1; i <= NF; i++) {
        if (index($i, prefix) == 1) {
          print substr($i, length(prefix) + 1)
          exit
        }
      }
      exit
    }
  ' "$PACKAGES_FILE"
}

normalize_dependency() {
  local dependency="$1"
  dependency="${dependency%%|*}"
  dependency="${dependency%% (*}"
  dependency="${dependency%%:*}"
  trim "$dependency"
}

mark_resolved() {
  local package="$1"
  grep -qx "$package" "$RESOLVED_FILE" 2>/dev/null && return 0
  printf '%s\n' "$package" >> "$RESOLVED_FILE"
}

resolve_package() {
  local package="$1"
  if grep -qx "$package" "$SEEN_FILE" 2>/dev/null; then
    return 0
  fi
  printf '%s\n' "$package" >> "$SEEN_FILE"

  local filename
  filename="$(package_field "$package" "Filename")"
  if [ -z "$filename" ]; then
    log "Could not find Debian package metadata for ${package}"
    exit 1
  fi

  local deps dep normalized
  deps="$(package_field "$package" "Pre-Depends")"
  deps="${deps},$(package_field "$package" "Depends")"
  printf '%s' "$deps" | tr ',' '\n' | while IFS= read -r dep; do
    normalized="$(normalize_dependency "$dep")"
    if [ -n "$normalized" ]; then
      resolve_package "$normalized"
    fi
  done

  mark_resolved "$package"
}

resolve_requested_packages() {
  SEEN_FILE="${CACHE_DIR}/resolved-seen.txt"
  RESOLVED_FILE="${CACHE_DIR}/resolved-packages.txt"
  : > "$SEEN_FILE"
  : > "$RESOLVED_FILE"

  local package
  for package in $REQUESTED_PACKAGES; do
    resolve_package "$package"
  done
}

download_package() {
  local package="$1"
  local filename sha256 deb_path url actual_sha
  filename="$(package_field "$package" "Filename")"
  sha256="$(package_field "$package" "SHA256")"
  deb_path="${CACHE_DIR}/debs/${package}.deb"
  url="${DEBIAN_MIRROR}/${filename}"

  mkdir -p "${CACHE_DIR}/debs"
  if [ -f "$deb_path" ] && [ -n "$sha256" ]; then
    actual_sha="$(shasum -a 256 "$deb_path" | awk '{ print $1 }')"
    if [ "$actual_sha" = "$sha256" ]; then
      printf '%s' "$deb_path"
      return 0
    fi
  fi

  log "Downloading Linux package: ${package}"
  curl -fsSL "$url" -o "$deb_path"
  if [ -n "$sha256" ]; then
    actual_sha="$(shasum -a 256 "$deb_path" | awk '{ print $1 }')"
    if [ "$actual_sha" != "$sha256" ]; then
      log "SHA256 mismatch for ${package}"
      exit 1
    fi
  fi
  printf '%s' "$deb_path"
}

extract_deb() {
  local deb_path="$1"
  local tmp_dir data_tar
  tmp_dir="$(mktemp -d "${CACHE_DIR}/extract.XXXXXX")"
  (
    cd "$tmp_dir"
    ar x "$deb_path"
  )
  data_tar="$(find "$tmp_dir" -name 'data.tar.*' -print -quit)"
  if [ -z "$data_tar" ]; then
    log "Could not find data archive in ${deb_path}"
    exit 1
  fi
  tar -xf "$data_tar" -C "$SYSROOT"
  rm -rf "$tmp_dir"
}

ensure_download_cache() {
  mkdir -p "$CACHE_DIR"
  copy_legacy_cache_if_present
  download_package_index
  resolve_requested_packages

  while IFS= read -r package; do
    [ -n "$package" ] || continue
    download_package "$package" >/dev/null
  done < "$RESOLVED_FILE"

  stamp_contents > "$DOWNLOAD_STAMP_FILE"
}

populate_sysroot() {
  mkdir -p "$SYSROOT" "$CACHE_DIR"
  ensure_download_cache
  resolve_requested_packages

  local package
  while IFS= read -r package; do
    [ -n "$package" ] || continue
    extract_deb "$(download_package "$package")"
  done < "$RESOLVED_FILE"

  write_env_file
  stamp_contents > "$STAMP_FILE"
}

CACHE_LOCK_HELD=0
SYSROOT_LOCK_HELD=0

cleanup_locks() {
  if [ "$CACHE_LOCK_HELD" -eq 1 ]; then
    rmdir "$CACHE_LOCK_DIR" 2>/dev/null || true
  fi
  if [ "$SYSROOT_LOCK_HELD" -eq 1 ]; then
    rmdir "$SYSROOT_LOCK_DIR" 2>/dev/null || true
  fi
}

acquire_cache_lock() {
  mkdir -p "$CACHE_DIR"
  while ! mkdir "$CACHE_LOCK_DIR" 2>/dev/null; do
    log "Waiting for Linux package cache lock: ${CACHE_DIR}"
    sleep 2
  done
  CACHE_LOCK_HELD=1
}

release_cache_lock() {
  if [ "$CACHE_LOCK_HELD" -eq 1 ]; then
    rmdir "$CACHE_LOCK_DIR" 2>/dev/null || true
    CACHE_LOCK_HELD=0
  fi
}

trap cleanup_locks EXIT

mkdir -p "$SYSROOT"
if sysroot_is_ready; then
  if ! download_cache_is_ready; then
    acquire_cache_lock
    ensure_download_cache
    release_cache_lock
  fi
  log "Using cached Linux sysroot: ${SYSROOT}"
  exit 0
fi

while ! mkdir "$SYSROOT_LOCK_DIR" 2>/dev/null; do
  log "Waiting for Linux sysroot lock: ${SYSROOT}"
  sleep 2
  if sysroot_is_ready; then
    if ! download_cache_is_ready; then
      acquire_cache_lock
      ensure_download_cache
      release_cache_lock
    fi
    log "Using cached Linux sysroot: ${SYSROOT}"
    exit 0
  fi
done
SYSROOT_LOCK_HELD=1

if sysroot_is_ready; then
  if ! download_cache_is_ready; then
    acquire_cache_lock
    ensure_download_cache
    release_cache_lock
  fi
  log "Using cached Linux sysroot: ${SYSROOT}"
  exit 0
fi

log "Preparing Linux sysroot: ${SYSROOT}"
log "Using Linux package download cache: ${CACHE_DIR}"
acquire_cache_lock
populate_sysroot
release_cache_lock

if ! sysroot_is_ready; then
  log "Linux sysroot was prepared but required pkg-config files are missing"
  exit 1
fi

log "Linux sysroot ready: ${SYSROOT}"
