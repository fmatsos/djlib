#!/usr/bin/env bash
# Creates a ready-to-use djlib Proxmox LXC container from scratch: provisions
# the container, mounts the source archive read-only and the djlib state
# directory read/write (docs/superpowers/specs/2026-08-15-djlib-milestone-1-
# catalog-dedup-design.md sec 3.1), upgrades packages, installs every system
# requirement (bootstrap.sh), then installs djlib itself, writes the default
# config and migrates the database (install-djlib.sh).
#
# Run as root on the Proxmox VE host. Safe to re-run: an existing container
# with the same CTID is reused and only updated, never recreated.
#
# Remote install, no local clone needed (like the Proxmox community
# scripts): this script fetches its sibling helpers (configure-mounts.sh,
# bootstrap.sh, install-djlib.sh) straight from GitHub when they aren't
# found next to it, so a bare curl + bash is enough:
#
#   CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/fmatsos/djlib/main/infra/lxc/create-container.sh)"
#
# Configuration is via environment variables, all optional except CTID:
#   CTID             container ID (required)
#   HOSTNAME         container hostname                 (default: djlib)
#   STORAGE          storage for the container rootfs    (default: local-lvm)
#   TEMPLATE_STORAGE storage holding the OS template      (default: local)
#   TEMPLATE         OS template to use. Either a full volid (e.g.
#                    local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst) or
#                    just its filename/basename within TEMPLATE_STORAGE (e.g.
#                    debian-13-standard_13.1-2_amd64, with or without the
#                    .tar.zst/.tar.gz/.tar.xz extension) -- run
#                    `pveam list <TEMPLATE_STORAGE>` to see exact names.
#                    (default: highest-version debian-<N>-standard already
#                    downloaded into TEMPLATE_STORAGE for this host's
#                    architecture, else the latest one available to download)
#   ARCH             container architecture to match when auto-selecting a
#                    template                            (default: host arch,
#                    via `dpkg --print-architecture`)
#   ROOTFS_SIZE_GB   rootfs size in GB                    (default: 8)
#   MEMORY_MB        RAM in MB                            (default: 2048)
#   SWAP_MB          swap in MB                           (default: 512)
#   CORES            CPU cores                            (default: 2)
#   BRIDGE           network bridge                       (default: vmbr0)
#   NET_CONFIG       full pct --net0 string
#                    (default: name=eth0,bridge=$BRIDGE,ip=dhcp)
#   MUSIC_SRC        host path with the source DJ archive  (default: /mnt/tank/djing)
#   DATA_SRC         host path for djlib state              (default: /mnt/tank/djlib)
#   DJLIB_REPO_URL   git remote to install djlib from
#                    (default: https://github.com/fmatsos/djlib.git)
#   DJLIB_REPO_REF   git ref (branch/tag) to install, and to fetch this
#                    script's own siblings from when run remotely
#                    (default: main)
#   DJLIB_RAW_BASE   raw-content base URL used to fetch missing siblings
#                    (default: https://raw.githubusercontent.com/fmatsos/djlib)
#
# Example (repo already cloned locally):
#   CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
#     ./infra/lxc/create-container.sh
set -Eeuo pipefail

CTID="${CTID:?Usage: CTID=<id> ./create-container.sh}"
CT_HOSTNAME="${HOSTNAME:-djlib}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
TEMPLATE="${TEMPLATE:-}"
ROOTFS_SIZE_GB="${ROOTFS_SIZE_GB:-8}"
MEMORY_MB="${MEMORY_MB:-2048}"
SWAP_MB="${SWAP_MB:-512}"
CORES="${CORES:-2}"
BRIDGE="${BRIDGE:-vmbr0}"
NET_CONFIG="${NET_CONFIG:-name=eth0,bridge=${BRIDGE},ip=dhcp}"
MUSIC_SRC="${MUSIC_SRC:-/mnt/tank/djing}"
DATA_SRC="${DATA_SRC:-/mnt/tank/djlib}"
DJLIB_REPO_URL="${DJLIB_REPO_URL:-https://github.com/fmatsos/djlib.git}"
DJLIB_REPO_REF="${DJLIB_REPO_REF:-main}"
DJLIB_RAW_BASE="${DJLIB_RAW_BASE:-https://raw.githubusercontent.com/fmatsos/djlib}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"

if [ -n "${ARCH:-}" ]; then
  : # explicit override
elif command -v dpkg >/dev/null 2>&1; then
  ARCH="$(dpkg --print-architecture)"
else
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64 | arm64) ARCH=arm64 ;;
    *) ARCH="$(uname -m)" ;;
  esac
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root on the Proxmox VE host" >&2
  exit 1
fi
command -v pct >/dev/null 2>&1 || {
  echo "error: pct not found -- run this on a Proxmox VE host" >&2
  exit 1
}

# Allocated unconditionally (cheap) rather than lazily: fetch_or_local runs
# inside a `$(...)` command substitution, i.e. a subshell, so it cannot
# populate this path lazily for the parent shell's cleanup trap to see.
FETCH_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$FETCH_TMP_DIR"' EXIT

# Resolves the path to a sibling helper script: next to this script when run
# from a local clone, otherwise fetched from GitHub (so this script also
# works piped straight into bash, with no local clone at all).
fetch_or_local() {
  local name="$1"
  if [ -n "$SCRIPT_DIR" ] && [ -r "$SCRIPT_DIR/$name" ]; then
    printf '%s\n' "$SCRIPT_DIR/$name"
    return
  fi
  command -v curl >/dev/null 2>&1 || {
    echo "error: curl not found -- required to fetch $name remotely" >&2
    exit 1
  }
  curl -fsSL "$DJLIB_RAW_BASE/$DJLIB_REPO_REF/infra/lxc/$name" -o "$FETCH_TMP_DIR/$name"
  chmod +x "$FETCH_TMP_DIR/$name"
  printf '%s\n' "$FETCH_TMP_DIR/$name"
}

CONFIGURE_MOUNTS="$(fetch_or_local configure-mounts.sh)"
BOOTSTRAP="$(fetch_or_local bootstrap.sh)"
INSTALL_DJLIB="$(fetch_or_local install-djlib.sh)"

if [ ! -d "$MUSIC_SRC" ]; then
  echo "error: MUSIC_SRC ($MUSIC_SRC) does not exist -- point it at the source DJ archive" >&2
  exit 1
fi
mkdir -p "$DATA_SRC"

# Resolves a user-supplied TEMPLATE (bare filename/basename, with or without
# the storage prefix and/or archive extension) to the full volid pct create
# needs. Passes an already-qualified volid ("storage:vztmpl/...") through
# unchanged.
resolve_template() {
  local requested="$1" match
  case "$requested" in
    *:*)
      printf '%s\n' "$requested"
      return
      ;;
  esac
  match="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk -v want="$requested" '
    {
      volid = $1
      n = split(volid, parts, "/")
      fname = parts[n]
      base = fname
      sub(/\.tar\.(zst|gz|xz)$/, "", base)
      if (fname == want || base == want) print volid
    }' | sort -V | tail -n1)"
  if [ -n "$match" ]; then
    printf '%s\n' "$match"
    return
  fi
  echo "error: template '$requested' not found in storage '$TEMPLATE_STORAGE'." >&2
  echo "Available templates in '$TEMPLATE_STORAGE':" >&2
  pveam list "$TEMPLATE_STORAGE" >&2 || true
  echo "Set TEMPLATE to one of the names above (with or without the" >&2
  echo "storage:vztmpl/ prefix and archive extension), or leave it unset" >&2
  echo "to auto-select a debian-standard template for arch '$ARCH'." >&2
  exit 1
}

if [ -n "$TEMPLATE" ]; then
  TEMPLATE="$(resolve_template "$TEMPLATE")"
else
  TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk -v arch="$ARCH" \
    '$1 ~ /vztmpl\/debian-[0-9][0-9]*-standard_/ && index($1, "_" arch ".") > 0 {print $1}' \
    | sort -V | tail -n1)"
fi
if [ -z "$TEMPLATE" ]; then
  pveam update
  LATEST="$(pveam available --section system 2>/dev/null | awk -v arch="$ARCH" \
    '$0 ~ /debian-[0-9][0-9]*-standard_/ && index($0, "_" arch ".") > 0 {print $2}' \
    | sort -V | tail -n1)"
  [ -n "$LATEST" ] || {
    echo "error: no debian-standard template available for arch '$ARCH' -- set TEMPLATE explicitly" >&2
    exit 1
  }
  pveam download "$TEMPLATE_STORAGE" "$LATEST"
  TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${LATEST}"
fi

if pct status "$CTID" >/dev/null 2>&1; then
  echo "container $CTID already exists -- reusing it"
else
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --unprivileged 1 \
    --memory "$MEMORY_MB" \
    --swap "$SWAP_MB" \
    --cores "$CORES" \
    --rootfs "${STORAGE}:${ROOTFS_SIZE_GB}" \
    --net0 "$NET_CONFIG" \
    --onboot 1
fi

"$CONFIGURE_MOUNTS" "$CTID"

pct start "$CTID"

echo "waiting for container network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

pct push "$CTID" "$BOOTSTRAP" /root/bootstrap.sh --perms 0755
pct push "$CTID" "$INSTALL_DJLIB" /root/install-djlib.sh --perms 0755

pct exec "$CTID" -- bash -c 'apt-get update && apt-get -y upgrade'
pct exec "$CTID" -- /root/bootstrap.sh
pct exec "$CTID" -- env \
  "DJLIB_REPO_URL=$DJLIB_REPO_URL" \
  "DJLIB_REPO_REF=$DJLIB_REPO_REF" \
  /root/install-djlib.sh

cat <<EOF

djlib container $CTID ("$CT_HOSTNAME") is ready.

  pct enter $CTID
  djlib doctor
  djlib scan

/music (read-only, from $MUSIC_SRC) and /data (from $DATA_SRC) are mounted;
config lives at /etc/djlib/config.toml (see config.example.toml).

Re-run this script any time to pull package updates and the latest djlib
release into the same container.
EOF
