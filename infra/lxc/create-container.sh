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
# Configuration is via environment variables, all optional except CTID:
#   CTID             container ID (required)
#   HOSTNAME         container hostname                 (default: djlib)
#   STORAGE          storage for the container rootfs    (default: local-lvm)
#   TEMPLATE_STORAGE storage holding the OS template      (default: local)
#   TEMPLATE         full template volid, e.g.
#                    local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst
#                    (default: latest downloaded/available debian-12-standard)
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
#   DJLIB_REPO_REF   git ref (branch/tag) to install        (default: main)
#
# Example:
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root on the Proxmox VE host" >&2
  exit 1
fi
command -v pct >/dev/null 2>&1 || {
  echo "error: pct not found -- run this on a Proxmox VE host" >&2
  exit 1
}

if [ ! -d "$MUSIC_SRC" ]; then
  echo "error: MUSIC_SRC ($MUSIC_SRC) does not exist -- point it at the source DJ archive" >&2
  exit 1
fi
mkdir -p "$DATA_SRC"

if [ -z "$TEMPLATE" ]; then
  TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '/debian-12-standard/{print $1}' | sort -V | tail -n1)"
fi
if [ -z "$TEMPLATE" ]; then
  pveam update
  LATEST="$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | sort -V | tail -n1)"
  [ -n "$LATEST" ] || {
    echo "error: no debian-12-standard template available -- set TEMPLATE explicitly" >&2
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

"$SCRIPT_DIR/configure-mounts.sh" "$CTID"

pct start "$CTID"

echo "waiting for container network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

pct push "$CTID" "$SCRIPT_DIR/bootstrap.sh" /root/bootstrap.sh --perms 0755
pct push "$CTID" "$SCRIPT_DIR/install-djlib.sh" /root/install-djlib.sh --perms 0755

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
