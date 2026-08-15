#!/usr/bin/env bash
set -Eeuo pipefail
CTID="${1:?Usage: configure-mounts.sh <ctid>}"
pct set "$CTID" -mp0 /mnt/tank/djing,mp=/music,ro=1
pct set "$CTID" -mp1 /mnt/tank/djlib,mp=/data
